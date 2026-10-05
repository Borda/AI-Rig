"""Composite verbs: diff impact against git, and batched sub-queries."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from codemap_py import query_state as state

# Transitional seam: exclusion rules live in codemap_py.scanner, but this
# module still reaches them through the old bare-name ``_exclusions`` import
# (bin/_exclusions.py, itself a shim onto codemap_py.scanner) via a
# bin/-relative sys.path insert, the same route bin/scan-index used to take.
# Every other import below is a direct package-internal import.
# parents[3] not [2]: this file sits one level deeper than the pre-split query.py
_BIN = Path(__file__).resolve().parents[3] / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))
from .coverage import _cmd_coverage
from .errors import _EXIT_BAD_INPUT, _EXIT_GENERIC, _die_json
from .index_io import _GIT_TIMEOUT_S
from .output import _print

# Reverse-dependency count thresholds mapping a module to a blast-radius risk tier.
# Matches the develop plugin's convention so a diff-impact tier reads the same as the
# per-module rdeps sizing used elsewhere: 5+ importers reach far (HIGH), 1–4 are
# contained (MODERATE), a leaf with no importers is self-contained (LOW).
_RISK_HIGH_MIN_RDEPS = 5


def _risk_tier(rdep_count: int) -> str:
    """Map a module's reverse-dependency count to a blast-radius risk tier.

    Examples:
        >>> _risk_tier(9)
        'HIGH'
        >>> _risk_tier(5)
        'HIGH'
        >>> _risk_tier(4)
        'MODERATE'
        >>> _risk_tier(1)
        'MODERATE'
        >>> _risk_tier(0)
        'LOW'
    """
    if rdep_count >= _RISK_HIGH_MIN_RDEPS:
        return "HIGH"
    if rdep_count >= 1:
        return "MODERATE"
    return "LOW"


def _git_diff_paths(base: str) -> list[str] | dict:
    """Return changed ``.py`` paths for *base*, or an error dict when git fails.

    ``base == "HEAD"`` (the default) diffs the working tree against HEAD — staged and
    unstaged changes both count, so a change is visible the moment it is written, before
    commit. Any other *base* is passed straight to ``git diff <base>`` so a caller can
    scope to a range (``main...HEAD``) or a single ref.

    Args:
        base: git ref or range to diff against; ``"HEAD"`` for the working tree.
    """
    cmd = ["git", "diff", "--name-only", base, "--", "*.py"]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=_GIT_TIMEOUT_S)
    except subprocess.CalledProcessError as exc:
        return {"error": "git diff failed", "base": base, "detail": f"exit {exc.returncode}"}
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"error": "git unavailable", "base": base, "detail": str(exc)}
    return [line for line in out.strip().splitlines() if line]


def _git_diff_line_ranges(base: str, path: str) -> list[tuple[int, int]]:
    """Return the changed line ranges in *path* for *base* as ``(start, end)`` tuples.

    Parses the ``@@ -a,b +c,d @@`` hunk headers of a zero-context diff. The ``+`` side
    (the post-change line numbers) is used because it aligns with the current file's
    line numbering — the same numbering the index's symbol ``start_line``/``end_line``
    coordinates use. A pure deletion (``+c,0``) contributes no post-image lines and is
    skipped. Any git failure yields an empty list — the caller then treats every symbol
    in the file as potentially changed rather than crashing.

    Args:
        base: git ref or range to diff against.
        path: repo-relative file path to inspect.
    """
    cmd = ["git", "diff", "--unified=0", base, "--", path]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=_GIT_TIMEOUT_S)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []
    ranges: list[tuple[int, int]] = []
    for line in out.splitlines():
        if not line.startswith("@@"):
            continue
        match = re.search(r"\+(\d+)(?:,(\d+))?", line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if count == 0:
            continue
        ranges.append((start, start + count - 1))
    return ranges


def _symbols_in_ranges(module: dict, ranges: list[tuple[int, int]]) -> list[str]:
    """Return qnames of *module* symbols overlapping any changed line range.

    A symbol overlaps a change when its ``[start_line, end_line]`` span intersects a
    changed ``(start, end)`` range. When *ranges* is empty (git could not produce hunk
    detail) every symbol is returned — the conservative choice, since the alternative
    would silently drop function-level impact for that file.

    Args:
        module: module entry dict from the index (source of ``symbols``).
        ranges: changed post-image line ranges from :func:`_git_diff_line_ranges`.
    """
    qnames: list[str] = []
    for sym in module.get("symbols", []):
        s_start = sym.get("start_line", 0)
        s_end = sym.get("end_line", s_start)
        qual = sym.get("qualified_name")
        if not qual:
            continue
        if not ranges or any(s_start <= r_end and r_start <= s_end for r_start, r_end in ranges):
            qnames.append(f"{module['name']}::{qual}")
    return qnames


def _map_changed_files(
    index: dict,
    base: str,
    paths: list[str],
    ranges_by_path: dict[str, list[tuple[int, int]]] | None = None,
) -> tuple[list[dict], list[str]]:
    """Resolve changed *paths* to their index modules and changed symbols.

    Args:
        index: parsed codemap index dict.
        base: git ref/range the diff was taken against (for per-file line ranges).
        paths: changed ``.py`` file paths from :func:`_git_diff_paths`.
        ranges_by_path: pre-computed changed line ranges per path (from
            :func:`_parse_unified_diff` in ``--diff-file`` mode). When ``None``,
            ranges come from ``git diff`` per file.

    Returns:
        ``(modules, unmapped)`` where ``modules`` is a list of
        ``{"module", "path", "changed_symbols"}`` for files present in the index, and
        ``unmapped`` is the paths that changed but are not indexed (new/untracked file,
        or a file the index excludes) — reported so the caller never hides them.
    """
    by_path = {m.get("path", ""): m for m in index.get("modules", []) if m.get("path")}
    modules: list[dict] = []
    unmapped: list[str] = []
    for path in paths:
        entry = by_path.get(path)
        if entry is None:
            unmapped.append(path)
            continue
        ranges = ranges_by_path.get(path, []) if ranges_by_path is not None else _git_diff_line_ranges(base, path)
        modules.append(
            {
                "module": entry["name"],
                "path": path,
                "changed_symbols": _symbols_in_ranges(entry, ranges),
            }
        )
    return modules, unmapped


def _parse_unified_diff(text: str) -> dict[str, list[tuple[int, int]]]:
    """Map each ``.py`` file in a unified diff to its changed post-image line ranges.

    Feeds ``--diff-file`` mode: a PR reviewed from a fetched diff (``gh pr diff``)
    has no local git objects, so ranges must come from the diff text itself. Paths
    are taken from ``+++ b/<path>`` lines (post-image side — the numbering the
    index's symbol coordinates use); ranges from ``@@ -a,b +c,d @@`` headers. Pure
    deletions (``+c,0``) contribute no post-image lines and are skipped; a deleted
    file (``+++ /dev/null``) is dropped entirely. A file that appears with no
    parsable ranges keeps an empty list — downstream treats that as "all symbols
    potentially changed" (same conservative fallback as the git path).

    Args:
        text: full unified-diff text (``git diff`` / ``gh pr diff`` format).

    Examples:
        >>> d = "diff --git a/pkg/m.py b/pkg/m.py\\n--- a/pkg/m.py\\n+++ b/pkg/m.py\\n@@ -1,2 +3,4 @@ def f():\\n"
        >>> _parse_unified_diff(d)
        {'pkg/m.py': [(3, 6)]}
        >>> _parse_unified_diff("+++ /dev/null\\n@@ -1,5 +0,0 @@\\n")
        {}
        >>> _parse_unified_diff("+++ b/doc/x.md\\n@@ -1 +1 @@\\n")
        {}
    """
    ranges_by_path: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].split("\t")[0].strip()
            if target == "/dev/null":
                current = None
                continue
            path = target[2:] if target.startswith(("a/", "b/")) else target
            current = path if path.endswith(".py") else None
            if current is not None:
                ranges_by_path.setdefault(current, [])
        elif line.startswith("@@") and current is not None:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if not match:
                continue
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            if count == 0:
                continue
            ranges_by_path[current].append((start, start + count - 1))
    return ranges_by_path


def _diff_impact_for_module(
    index: dict,
    parser: argparse.ArgumentParser,
    project_root: Path,
    changed: dict,
) -> dict:
    """Compute the blast radius for one changed module via reused sub-queries.

    Runs ``rdeps`` and ``coupled`` for the module and ``fn-rdeps`` for each of its
    changed symbols through :func:`_run_subquery` — the same in-process path ``batch``
    uses — then derives a risk tier from the reverse-dependency count. A sub-query that
    errors is folded into a per-module ``errors`` list (never fatal): one unresolvable
    module must not abort the whole diff-impact run.

    Args:
        index: parsed codemap index dict.
        parser: top-level argparse parser, reused to run sub-queries.
        project_root: resolved project root for file-path lookups.
        changed: one ``{"module", "path", "changed_symbols"}`` entry from
            :func:`_map_changed_files`.
    """
    module = changed["module"]
    errors: list[dict] = []
    rdeps_payload, _ = _run_subquery(index, parser, project_root, ["rdeps", module])
    if "error" in rdeps_payload:
        errors.append({"query": "rdeps", "module": module, "error": rdeps_payload["error"]})
    importers = rdeps_payload.get("imported_by", []) if "error" not in rdeps_payload else []
    rdep_count = len(importers)

    coupled_internal: int | None = None
    coupled_payload, _ = _run_subquery(index, parser, project_root, ["coupled", "--top", "0"])
    if "error" not in coupled_payload:
        for row in coupled_payload.get("coupled", []):
            if row.get("name") == module:
                coupled_internal = row.get("internal_dep_count", 0)
                break

    fn_rdeps: list[dict] = []
    for qname in changed["changed_symbols"]:
        payload, _ = _run_subquery(index, parser, project_root, ["fn-rdeps", qname])
        if "error" in payload:
            errors.append({"query": "fn-rdeps", "qname": qname, "error": payload["error"]})
            continue
        fn_rdeps.append({"qname": qname, "caller_count": payload.get("count", 0)})

    result = {
        "module": module,
        "path": changed["path"],
        "changed_symbols": changed["changed_symbols"],
        "rdep_count": rdep_count,
        "importers": importers,
        "coupled_internal_deps": coupled_internal,
        "fn_rdeps": fn_rdeps,
        "risk": _risk_tier(rdep_count),
    }
    if errors:
        result["errors"] = errors
    return result


def _diff_impact_tests(
    index: dict,
    parser: argparse.ArgumentParser,
    project_root: Path,
    targets: list[str],
) -> dict:
    """Union the ``test-impact`` sets across every changed module and symbol.

    Each target (a module or ``module::symbol``) is run through ``test-impact`` and the
    resulting test files are unioned, so the caller gets one deduplicated pytest target
    set for the whole change rather than a per-symbol scatter. Errored targets are
    silently skipped here — their failure is already surfaced per-module in
    :func:`_diff_impact_for_module`.

    Args:
        index: parsed codemap index dict.
        parser: top-level argparse parser, reused to run sub-queries.
        project_root: resolved project root for file-path lookups.
        targets: module and ``module::symbol`` strings to union test impact over.
    """
    test_files: set[str] = set()
    for target in targets:
        payload, _ = _run_subquery(index, parser, project_root, ["test-impact", target])
        if "error" in payload:
            continue
        test_files.update(payload.get("test_files", []))
    files = sorted(test_files)
    return {
        "test_files": files,
        "total": len(files),
        "pytest_cmd": ("pytest " + " ".join(files)) if files else "",
    }


def cmd_diff_impact(index: dict, args: argparse.Namespace, parser: argparse.ArgumentParser, project_root: Path) -> None:
    """Report the structural blast radius of the current git change set in one JSON object.

    Diffs the working tree (or a ``--base REF`` range) to find changed ``.py`` files,
    maps each to its indexed module and the symbols whose line ranges the change
    touched, then reuses the in-process sub-query path (the same machinery ``batch``
    uses) to run ``rdeps`` + ``coupled`` per changed module, ``fn-rdeps`` per changed
    symbol, and a unioned ``test-impact`` across the whole set. Each module is tagged
    with a risk tier from its reverse-dependency count (``HIGH`` ≥5, ``MODERATE`` 1–4,
    ``LOW`` 0). One coverage block is emitted for the whole result; a per-module
    sub-query failure is recorded in that module's ``errors`` list without aborting.

    Args:
        index: parsed codemap index dict.
        args: the diff-impact namespace; ``args.base`` is the ref to diff against,
            or ``args.diff_file`` a unified-diff file (``-`` = stdin) that replaces
            local git as the change-set source (PR-review mode).
        parser: top-level argparse parser, reused to run sub-queries.
        project_root: resolved project root for file-path lookups.
    """
    diff_file = getattr(args, "diff_file", None)
    if diff_file:
        try:
            diff_text = sys.stdin.read() if diff_file == "-" else Path(diff_file).read_text(errors="replace")
        except OSError as exc:
            _die_json({"error": "diff file unreadable", "path": diff_file, "detail": str(exc)}, _EXIT_BAD_INPUT)
        ranges_by_path = _parse_unified_diff(diff_text)
        paths: list[str] | dict = sorted(ranges_by_path)
        base_label = f"diff-file:{diff_file}"
    else:
        ranges_by_path = None
        paths = _git_diff_paths(args.base)
        base_label = args.base
    if isinstance(paths, dict):  # git failed — surface as a hard, actionable error
        _die_json(paths, _EXIT_GENERIC)
    changed_modules, unmapped = _map_changed_files(index, args.base, paths, ranges_by_path)

    impacts = [_diff_impact_for_module(index, parser, project_root, cm) for cm in changed_modules]
    targets = [cm["module"] for cm in changed_modules] + [q for cm in changed_modules for q in cm["changed_symbols"]]
    tests = _diff_impact_tests(index, parser, project_root, targets)
    highest = max((i["risk"] for i in impacts), key=("LOW", "MODERATE", "HIGH").index, default="LOW")

    _print(
        json.dumps(
            {
                "base": base_label,
                "changed_files": len(paths),
                "changed_modules": impacts,
                "unmapped_files": unmapped,
                "test_impact": tests,
                "highest_risk": highest,
                "index": _cmd_coverage(index, method="static-ast", scope="diff-impact"),
            }
        )
    )


def _load_batch_items(source: str) -> list[dict]:
    """Read and validate the batch request array from inline JSON, a file path, or stdin.

    A caller who passes the array itself rather than a path used to see its own JSON reported as a
    missing filename, which reads as a broken command rather than a wrong argument form. An argument
    that already starts with ``[`` is therefore taken as the array, and the unreadable-input error
    names every accepted form instead of only the filesystem failure.

    Args:
        source: the JSON array itself, a filesystem path to a JSON file, or ``"-"`` to read stdin.

    Returns:
        The parsed list of request objects.

    Raises:
        SystemExit: via :func:`_die_json` (exit 2) on unreadable input, non-JSON,
            or a top-level value that is not a list.
    """
    if source.lstrip().startswith("["):
        raw = source
    else:
        try:
            raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        except OSError as exc:
            _die_json(
                {
                    "error": "batch input unreadable",
                    "detail": str(exc),
                    "accepts": "a JSON array, a path to a JSON file, or '-' for stdin",
                },
                _EXIT_BAD_INPUT,
            )
    try:
        items = json.loads(raw)
    except ValueError as exc:
        _die_json({"error": "batch input is not valid JSON", "detail": str(exc)}, _EXIT_BAD_INPUT)
    if not isinstance(items, list):
        _die_json({"error": "batch input must be a JSON array of {cmd, args} objects"}, _EXIT_BAD_INPUT)
    return items


# Composite commands that run their own in-process sub-queries; nesting either inside
# a batch item would recurse the capture buffer and is rejected. Kept as a set so
# diff-impact joins batch under one guard rather than a growing chain of ``==`` checks.
_NON_NESTABLE_IN_BATCH = frozenset({"batch", "diff-impact"})


def _batch_item_argv(item: object) -> list[str] | dict:
    """Turn one batch request object into an argv list, or return an error dict.

    A valid item is ``{"cmd": "<subcommand>", "args": ["...", ...]}`` where ``args``
    is optional and defaults to ``[]``. ``cmd`` must be a non-empty string and must not
    be a composite command that runs its own sub-queries (:data:`_NON_NESTABLE_IN_BATCH`
    — ``batch``, ``diff-impact``). Every token is coerced to ``str`` so a caller may
    pass ``{"cmd": "central", "args": ["--top", 5]}`` with a numeric arg.

    Args:
        item: one element of the decoded batch array.

    Returns:
        The argv list (``[cmd, *args]``) on success, else an ``{"error": ...}`` dict.
    """
    if not isinstance(item, dict):
        return {"error": "batch item must be an object with a 'cmd' key", "item": item}
    cmd = item.get("cmd")
    if not cmd or not isinstance(cmd, str):
        return {"error": "batch item missing string 'cmd'", "item": item}
    if cmd in _NON_NESTABLE_IN_BATCH:
        return {"error": f"'{cmd}' cannot be nested inside batch", "cmd": cmd}
    raw_args = item.get("args", [])
    if not isinstance(raw_args, list):
        return {"error": "batch item 'args' must be a list", "cmd": cmd}
    return [cmd, *(str(a) for a in raw_args)]


def _run_subquery(
    index: dict, parser: argparse.ArgumentParser, project_root: Path, argv: list[str]
) -> tuple[dict, dict | None]:
    """Run one scan-query subcommand in-process and return its decoded result.

    The reuse core behind both ``batch`` and ``diff-impact``: parse *argv* through the
    top-level *parser*, divert the handler's stdout into a capture buffer via
    :data:`_capture`, run it through the same :func:`_dispatch_command` path a
    standalone invocation uses, and decode the single JSON object it printed. A handler
    that exits via :func:`_die_json` / :func:`_exit_error` leaves its error object in
    the buffer, which is decoded and returned like any other payload — the caller sees
    a ``{"error": ...}`` dict rather than a process exit. A bad subcommand or flag that
    argparse rejects returns a synthetic ``invalid command or arguments`` error.

    Args:
        index: parsed codemap index dict.
        parser: the top-level argparse parser, reused to parse *argv*.
        project_root: resolved project root for file-path lookups.
        argv: the sub-invocation argument vector (``[subcommand, *args]``).

    Returns:
        ``(payload, coverage)`` where ``payload`` is the handler's JSON minus its
        ``index`` coverage block, and ``coverage`` is that block (or None when the
        handler emitted none, e.g. on an error before coverage was built).
    """
    try:
        sub_args = parser.parse_args(argv)
    except SystemExit:
        return {"error": "invalid command or arguments"}, None
    buf: list[str] = []
    state._capture = buf
    # Imported here, not at module scope: cli imports this module for cmd_batch, so a
    # top-level import back into cli is a circular import at package load.
    from .cli import _dispatch_command

    try:
        _dispatch_command(index, sub_args, parser, project_root)
    except SystemExit:
        # A handler hit _die_json / _exit_error; its JSON error object is already in
        # buf. Fall through to decode it as this sub-query's result.
        pass
    finally:
        state._capture = None
    payload = json.loads(buf[-1]) if buf else {"error": "no output"}
    coverage = payload.pop("index", None)
    return payload, coverage


def cmd_batch(index: dict, args: argparse.Namespace, parser: argparse.ArgumentParser, project_root: Path) -> None:
    """Run queries in-process while preserving each result's coverage and limits.

    Batch shares one index load and process. Each request uses the normal parser
    and dispatch path, and retains its own ``result.index`` metadata. The top-level
    ``index`` contains common coverage fields plus conservative completion and
    truncation summaries; it never substitutes for a particular item's metadata.

    Results preserve input order. A request that fails to parse, raises, or exits via
    :func:`_die_json` yields a per-item ``{"ok": false, "error": ...}`` object rather
    than aborting the batch — one bad query never kills the run.

    Args:
        index: parsed codemap index dict.
        args: the batch namespace; ``args.input`` is the file path or ``"-"``.
        parser: the top-level argparse parser, reused to parse each item's argv.
        project_root: resolved project root for file-path lookups.
    """
    items = _load_batch_items(args.input)
    results: list[dict] = []
    coverages: list[dict] = []
    for i, item in enumerate(items):
        argv = _batch_item_argv(item)
        if isinstance(argv, dict):  # malformed item — argv builder returned an error
            results.append({"ok": False, "index": i, "error": argv["error"], "detail": argv})
            continue
        payload, coverage = _run_subquery(index, parser, project_root, argv)
        # Scope and truncation differ between queries even against the same index.
        if coverage is not None:
            payload["index"] = coverage
            coverages.append(coverage)
        ok = "error" not in payload
        entry = {"ok": ok, "index": i, "cmd": argv[0], "result": payload}
        if not ok:
            # Contract: failed items expose a top-level "error" — batch consumers
            # (e.g. triage-batch staleness checks) parse it without unwrapping result.
            entry["error"] = payload["error"]
        results.append(entry)
    out: dict = {"batch": results, "count": len(results)}
    shared: dict = {}
    if coverages:
        shared = {
            key: value
            for key, value in coverages[0].items()
            if key not in {"total_available", "exhaustive", "completeness_reason", "note"}
            and all(key in coverage and coverage[key] == value for coverage in coverages)
        }
    # Per-query prose/legacy completeness cannot describe failed or missing siblings.
    complete = len(coverages) == len(results) and all(entry["ok"] for entry in results)
    shared["query_complete"] = complete and all(c.get("query_complete") is True for c in coverages)
    shared["truncated"] = any(c.get("truncated", False) for c in coverages)
    shared["confidence"] = (
        "exact"
        if shared["query_complete"]
        and not shared["truncated"]
        and all(c.get("confidence") == "exact" for c in coverages)
        else "partial"
    )
    out["index"] = shared
    _print(json.dumps(out))
