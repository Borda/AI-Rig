"""Function-level verbs over the call graph, including test impact and mocks."""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

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
from codemap_py.schema import MOCK_PATCHES_MIN_VER, VALID_CALL_RESOLUTIONS  # noqa: E402

from .coverage import _CALL_GRAPH_NOT_COVERED, _cmd_coverage  # noqa: E402
from .errors import _exit_symbol_not_found, _exit_target_not_found  # noqa: E402
from .index_io import (  # noqa: E402
    _get_rev_graph,
    _get_rev_import_graph,
    _get_symbol_map,
    _normalize_symbol_target,
    _require_call_graph,
    _require_feature,
    _resolve_symbol_alias,
    _SymbolTarget,
    build_module_map,
)
from .output import _print  # noqa: E402

#: Method suffix whose static callers the scanner records on the owning class: ``Foo()`` is an edge to ``mod::Foo``.
_INIT_SUFFIX = ".__init__"
#: ``not_covered`` slug added once constructor edges were merged: the index records no class bases and no
#: ``super().__init__()`` edge, so ``Sub()`` running ``Base.__init__`` is never followed.
_INHERITED_CONSTRUCTORS = "inherited-constructors"


def _call_graph_not_covered(merged: bool) -> list[str]:
    """Return the call-graph ``not_covered`` slugs, naming inherited constructors when a constructor union applied.

    Examples:
        >>> _call_graph_not_covered(False) == _CALL_GRAPH_NOT_COVERED
        True
        >>> _call_graph_not_covered(True)[-1]
        'inherited-constructors'
    """
    return [*_CALL_GRAPH_NOT_COVERED, _INHERITED_CONSTRUCTORS] if merged else _CALL_GRAPH_NOT_COVERED


def _resolve_fn_target(index: dict, sym_map: dict, target: str, *, accept_aliases: bool = False) -> _SymbolTarget:
    """Resolve an ``fn-*`` target to one indexed symbol, or exit with the not-found / ambiguous error.

    Args:
        index: parsed codemap index dict.
        sym_map: flat ``module::symbol`` lookup.
        target: caller-supplied ``module::symbol``, dotted ``module.symbol``, or bare/class-qualified name.
        accept_aliases: also accept persisted ``symbol_aliases`` keys (see :func:`_normalize_symbol_target`).

    Returns:
        The resolved target; never returns when nothing (or more than one symbol) matched.
    """
    resolved = _normalize_symbol_target(index, sym_map, target, accept_aliases=accept_aliases)
    if not resolved.found:
        _exit_symbol_not_found(index, target, resolved.candidates)
    return resolved


def _normalized_fields(target: _SymbolTarget) -> dict[str, str]:
    """Return the ``normalized_from`` payload field when the caller's spelling was rewritten, else nothing.

    The field is optional on purpose: an exact ``module::symbol`` query keeps its historic payload keys byte-for-byte.

    Examples:
        >>> _normalized_fields(_SymbolTarget("pkg::run", found=True, normalized_from="pkg.run"))
        {'normalized_from': 'pkg.run'}
        >>> _normalized_fields(_SymbolTarget("pkg::run", found=True))
        {}
    """
    return {"normalized_from": target.normalized_from} if target.normalized_from else {}


def _constructor_class(sym_map: dict, node: str) -> str | None:
    """Return the class whose ``Class()`` call edges stand for calls to *node*, when *node* is ``Class.__init__``.

    Args:
        sym_map: flat ``module::symbol`` lookup.
        node: a ``module::symbol`` qname.

    Returns:
        The owning class qname when *node* is an ``__init__`` method of an indexed class, else ``None``.

    Examples:
        >>> sym_map = {"pkg::Meter": None, "pkg::Meter.__init__": None}
        >>> _constructor_class(sym_map, "pkg::Meter.__init__")
        'pkg::Meter'
        >>> _constructor_class(sym_map, "pkg::Meter.update") is None
        True
        >>> _constructor_class(sym_map, "pkg::Other.__init__") is None
        True
    """
    module, sep, symbol = node.partition("::")
    if not sep or not symbol.endswith(_INIT_SUFFIX):
        return None
    class_qname = f"{module}::{symbol[: -len(_INIT_SUFFIX)]}"
    return class_qname if class_qname in sym_map else None


def _direct_callers(rev_graph: dict, sym_map: dict, node: str) -> tuple[set[str], bool]:
    """Return the distinct direct callers of *node* and whether constructor edges were merged in.

    The scanner records ``Foo()`` as a call to ``mod::Foo``, never to ``mod::Foo.__init__``. Without the union every
    ``Foo.__init__`` query reported zero callers while still claiming a complete answer, and every transitive walk
    stopped at the first constructor it reached.

    Args:
        rev_graph: reverse call map keyed by alias-resolved callee qname.
        sym_map: flat ``module::symbol`` lookup.
        node: the callee whose callers are wanted.

    Returns:
        ``(callers, merged)`` where ``merged`` is True when *node* is ``Class.__init__`` and the class's own
        ``Class()`` edges were added to the caller set.
    """
    callers = set(rev_graph.get(node, []))
    class_qname = _constructor_class(sym_map, node)
    if class_qname is None:
        return callers, False
    return callers | set(rev_graph.get(class_qname, [])), True


def _reverse_call_walk(rev_graph: dict, sym_map: dict, start: str) -> tuple[list[tuple[str, int]], bool]:
    """Walk transitive callers of *start* breadth-first, recording each caller at its shortest depth.

    Shared by ``fn-blast`` and function-level ``test-impact`` so both apply the same constructor-edge union at every
    node, not only at the start.

    Args:
        rev_graph: reverse call map keyed by alias-resolved callee qname.
        sym_map: flat ``module::symbol`` lookup.
        start: the ``module::symbol`` whose callers are traced; never itself reported.

    Returns:
        ``(reached, merged)``: every transitive caller with its depth in BFS order, and whether any visited
        ``Class.__init__`` node pulled in ``Class()`` constructor edges.
    """
    visited: set[str] = {start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    reached: list[tuple[str, int]] = []
    merged = False
    while queue:
        node, depth = queue.popleft()
        callers, node_merged = _direct_callers(rev_graph, sym_map, node)
        merged = merged or node_merged
        for caller in sorted(callers - visited):
            visited.add(caller)
            reached.append((caller, depth + 1))
            queue.append((caller, depth + 1))
    return reached, merged


def _is_dunder(name: str) -> bool:
    """Return whether *name* is a ``__protocol__`` method name, not a name-mangled ``__private`` one.

    Examples:
        >>> _is_dunder("__len__"), _is_dunder("__secret"), _is_dunder("____")
        (True, False, False)
    """
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _empty_method_hint(qname: str) -> str | None:
    r"""Explain a zero-caller method answer and name the reference search that finds what static analysis cannot see.

    Telemetry showed most zero-caller ``fn-rdeps`` answers were methods reached as ``obj.method()``: the receiver's
    class is unknown statically, so the edge is unresolved rather than absent. ``query_complete`` keeps its meaning;
    this only tells the reader why zero is expected and how to look further. ``fn-blast`` and function-level
    ``test-impact`` walk the same reverse edges, so an empty walk there gets the same hint.

    The search is reference-shaped, not call-shaped: a property read (``obj.val``) or a bound method passed as a
    callback (``register(obj.on_done)``) has no ``(`` after the name, so a ``.name(`` search reported "only the
    definition" for a method in active use. The escaped dot also keeps the ``def name(`` line itself out of the result.

    A protocol (dunder) method other than ``__init__`` gets no search at all: Python reaches ``__len__``, ``__eq__`` or
    ``__enter__`` through ``len(b)``, ``b == c`` or ``with b:``, never ``b.__len__``, so a ``\.__len__\b`` search came
    back empty for a method in active use — and an empty search was what licensed a manual delete. Its hint instead
    says never to delete the method on this answer.

    The action — the search, or the instruction to keep a protocol method — opens the hint and the explanation follows.
    A consumer that bounds strings cuts the tail: the Codex structural context keeps 300 characters, and with the
    action last it dropped the search of any method named longer than ``Report.render`` and the do-not-delete clause of
    every protocol method.

    Args:
        qname: ``module::symbol`` of the queried callee.

    Returns:
        Hint text for a ``Class.method`` target, else ``None`` for a module-level function.

    Examples:
        >>> print(_empty_method_hint("pkg::Meter.update").partition(". ")[0])
        Find references with grep -rnE "\.update\b"
        >>> print(_empty_method_hint("pkg::Meter.__init__").partition(". ")[0])
        Find references, subclasses included, with grep -rnE "\bMeter\b"
        >>> eq_hint = _empty_method_hint("pkg::Bag.__eq__")
        >>> eq_hint.startswith("Never delete protocol method Bag.__eq__ on zero callers."), "grep" in eq_hint
        (True, False)
        >>> print(_empty_method_hint("pkg::Box.__secret").partition(". ")[0])
        Find references with grep -rnE "\.__secret\b"
        >>> _empty_method_hint("pkg::helper") is None
        True
    """
    symbol = qname.partition("::")[2]
    owner, dot, method = symbol.rpartition(".")
    if not dot:
        return None
    if method == "__init__":
        class_name = owner.rpartition(".")[2]
        return (
            f'Find references, subclasses included, with grep -rnE "\\b{class_name}\\b". '
            f"0 static callers for constructor {symbol}: no {class_name}() call site is indexed, and subclass "
            "constructors and super().__init__() calls are never followed."
        )
    if _is_dunder(method):
        return (
            f"Never delete protocol method {symbol} on zero callers. Python calls {method} implicitly through "
            "syntax, operators and builtins (len(), ==, hash(), iteration, with, obj()), never as "
            f"obj.{method}, so neither the call graph nor a reference search sees those uses: 0 static callers is "
            "expected and never evidence that it is unused."
        )
    return (
        f'Find references with grep -rnE "\\.{method}\\b". '
        f"0 static callers for method {symbol}: instance calls (obj.{method}()), property reads (obj.{method}), "
        f"bound-method references (callback=obj.{method}), inherited or overridden methods, and other dynamic "
        "dispatch are not resolved to a class statically."
    )


def _is_collected_test_method(sym_map: dict, qname: str) -> bool:
    """Return whether *qname* is a test method a test runner collects and calls, so it has no caller in code.

    The rule is the default collection pattern of pytest (``python_functions = "test*"``) and unittest (``test*``
    methods of a ``TestCase``): a method whose name starts with ``test``, on a class in a test module. A project's own
    collection configuration is not read. The class name is not checked either: the index records no class bases, so a
    unittest ``TestCase`` subclass named ``FooTests`` could not be told apart from a pytest ``Test*`` class.

    Args:
        sym_map: qname-to-``(module entry, symbol)`` map from :func:`_get_symbol_map`.
        qname: ``module::symbol`` of the queried callee.

    Examples:
        >>> sym_map = {"tests.t::TestX.test_a": ({"is_test": True}, {}), "pkg::Link.test_a": ({"is_test": False}, {})}
        >>> [_is_collected_test_method(sym_map, qname) for qname in sym_map]
        [True, False]
        >>> _is_collected_test_method({"tests.t::test_plain": ({"is_test": True}, {})}, "tests.t::test_plain")
        False
    """
    _owner, dot, method = qname.partition("::")[2].rpartition(".")
    if not dot or not method.startswith("test"):
        return False
    entry = sym_map.get(qname)
    return bool(entry and entry[0].get("is_test"))


def _zero_caller_hint(sym_map: dict, qname: str) -> str | None:
    """Return the hint a reverse-call answer with no caller carries, or ``None`` when zero callers needs none.

    A collected test method gets none: the runner calls it, so zero static callers is the expected answer rather than
    an unresolved one, and the method hint's search finds nothing. Giving it the hint marked every changed test method
    in a ``diff-impact`` answer as unresolved and made a test-only change read as degraded evidence.

    Args:
        sym_map: qname-to-``(module entry, symbol)`` map from :func:`_get_symbol_map`.
        qname: ``module::symbol`` of the queried callee.
    """
    if _is_collected_test_method(sym_map, qname):
        return None
    return _empty_method_hint(qname)


def cmd_fn_deps(index: dict, qname: str) -> None:
    """List the functions called by a qualified function name.

    Args:
        index: parsed codemap index dict (must be v3 with call graph).
        qname: ``module::symbol``, dotted ``module.symbol``, or a unique bare/class-qualified symbol name.
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    target = _resolve_fn_target(index, sym_map, qname)
    _module_entry, sym = sym_map[target.qname]
    calls = [
        {"target": edge["target"], "resolution": edge.get("resolution", "")}
        for edge in sym.get("calls", [])
        if edge.get("resolution") in VALID_CALL_RESOLUTIONS
    ]
    _print(
        json.dumps(
            {
                "qname": target.qname,
                **_normalized_fields(target),
                "calls": calls,
                "count": len(calls),
                "index": _cmd_coverage(
                    index, method="static-ast", not_covered=["dynamic-dispatch", "runtime-injection"]
                ),
            }
        )
    )


def cmd_fn_rdeps(index: dict, qname: str, exclude_tests: bool = False) -> None:
    """List the functions that call a qualified function name.

    The caller list is deduplicated: each calling symbol appears at most once even
    when it calls *qname* from several call sites. ``count`` and its explicit alias
    ``unique_caller_count`` therefore both report the number of *distinct* callers,
    not the number of individual call-site edges.

    Optional payload keys appear only when they apply, so an ordinary ``module::symbol``
    query keeps its historic shape: ``normalized_from`` (the input was a dotted or bare
    name), ``constructor_callers_merged`` (an ``__init__`` target also counts ``Class()``
    call sites), and ``hint`` (a method with zero static callers).

    Args:
        index: parsed codemap index dict (must be v3 with call graph).
        qname: ``module::symbol``, dotted ``module.symbol``, or a unique bare/class-qualified symbol name.
        exclude_tests: drop callers that live in test modules.
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    target = _resolve_fn_target(index, sym_map, qname, accept_aliases=True)
    resolved_qname = _resolve_symbol_alias(index, target.qname)
    if resolved_qname is None:
        _exit_symbol_not_found(index, qname)
    caller_qnames, merged = _direct_callers(_get_rev_graph(index), sym_map, resolved_qname)
    callers = []
    for caller_qname in sorted(caller_qnames):
        entry = sym_map.get(caller_qname)
        m_entry = entry[0] if entry else {}
        callers.append(
            {
                "caller": caller_qname,
                "module": m_entry.get("name", ""),
                "path": m_entry.get("path", ""),
            }
        )
    # Decided before the test filter: a method called only from tests has static callers, so a "0 static callers ...
    # dynamic dispatch" hint would be false; --exclude-tests just hides them.
    hint = None if callers else _zero_caller_hint(sym_map, resolved_qname)
    if exclude_tests:
        callers = [c for c in callers if not sym_map.get(c["caller"], ({}, {}))[0].get("is_test")]
    fn_name = target.qname.split("::")[-1]
    payload: dict = {
        "qname": target.qname,
        **_normalized_fields(target),
        "resolved_qname": resolved_qname,
        "called_by": callers,
        "count": len(callers),
        "unique_caller_count": len(callers),
    }
    if merged:
        payload["constructor_callers_merged"] = True
    if hint:
        payload["hint"] = hint
    payload["index"] = _cmd_coverage(
        index,
        query_target=target.qname,
        method="static-ast",
        answer_hint=hint is not None,
        not_covered=_call_graph_not_covered(merged),
        hint=f'grep -rn "{fn_name}" to find hook-registered callers not in static AST',
    )
    _print(json.dumps(payload))


def cmd_fn_central(index: dict, top: int, exclude_tests: bool = False) -> None:
    """Most-called functions globally (by incoming call-edge count).

    Args:
        index: parsed codemap index dict (must be v3 with call graph).
        top: number of top-ranked functions to return.
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    # Count how many times each target appears across all forward call edges
    counts: dict[str, int] = {}
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        if exclude_tests and m.get("is_test"):
            continue
        for sym in m.get("symbols", []):
            for edge in sym.get("calls", []):
                if edge.get("resolution") in VALID_CALL_RESOLUTIONS:
                    target = _resolve_symbol_alias(index, edge["target"])
                    if target is None:
                        continue
                    counts[target] = counts.get(target, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top]
    result = []
    for qname, call_count in ranked:
        entry = sym_map.get(qname)
        m_entry = entry[0] if entry else {}
        result.append(
            {
                "qname": qname,
                "call_count": call_count,
                "module": m_entry.get("name", ""),
                "path": m_entry.get("path", ""),
            }
        )
    _print(
        json.dumps(
            {"fn_central": result, "index": _cmd_coverage(index, method="static-ast", scope="call-graph-centrality")}
        )
    )


def cmd_fn_blast(index: dict, qname: str) -> None:
    """Transitive reverse-call BFS from *qname* -- everything that calls X, directly or transitively.

    ``constructor_callers_merged`` is added only when the walk crossed a ``Class.__init__``
    node and followed that class's ``Class()`` call sites. ``hint`` is added only for a method
    or constructor the walk found no caller for, exactly as ``fn-rdeps`` does.

    Args:
        index: parsed codemap index dict (must be v3 with call graph).
        qname: ``module::symbol``, dotted ``module.symbol``, or a unique bare/class-qualified symbol name.
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    target = _resolve_fn_target(index, sym_map, qname)
    reached, merged = _reverse_call_walk(_get_rev_graph(index), sym_map, target.qname)
    hint = None if reached else _zero_caller_hint(sym_map, target.qname)
    result = []
    for caller, depth in reached:
        entry = sym_map.get(caller)
        m_entry = entry[0] if entry else {}
        result.append(
            {
                "caller": caller,
                "depth": depth,
                "module": m_entry.get("name", ""),
                "path": m_entry.get("path", ""),
            }
        )
    result.sort(key=lambda x: (x["depth"], x["caller"]))
    payload: dict = {
        "qname": target.qname,
        **_normalized_fields(target),
        "blast_radius": result,
        "total_callers": len(result),
    }
    if merged:
        payload["constructor_callers_merged"] = True
    if hint:
        payload["hint"] = hint
    payload["index"] = _cmd_coverage(
        index,
        query_target=target.qname,
        method="static-ast",
        scope="transitive-call-graph",
        answer_hint=hint is not None,
        not_covered=_call_graph_not_covered(merged),
    )
    _print(json.dumps(payload))


def _test_impact_via_function_call(index: dict, qname: str) -> tuple[set[str], set[str], bool, bool]:
    """BFS the reverse call graph from *qname*; return test files, test modules, and the walk's two flags.

    Args:
        index: parsed codemap index dict (must be v3+ with call graph).
        qname: ``module::symbol`` whose callers are traced transitively.

    Returns:
        ``(test_files, test_modules, merged, has_callers)``: ``merged`` is the constructor-merge flag, and
        ``has_callers`` is whether the walk reached any caller at all, test or not. Zero test files is not zero callers,
        so only ``has_callers`` decides whether the zero-caller method hint applies.
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    if qname not in sym_map:
        _exit_symbol_not_found(index, qname)
    reached, merged = _reverse_call_walk(_get_rev_graph(index), sym_map, qname)
    test_files: set[str] = set()
    test_mods: set[str] = set()
    for caller, _depth in reached:
        entry = sym_map.get(caller)
        if not entry or not entry[0].get("is_test"):
            continue
        if p := entry[0].get("path", ""):
            test_files.add(p)
        if n := entry[0].get("name", ""):
            test_mods.add(n)
    return test_files, test_mods, merged, bool(reached)


def _test_impact_function_target(index: dict, qname: str, module_map: dict) -> _SymbolTarget | None:
    """Pick function-level test impact for *qname*, or ``None`` to keep the module-level import walk.

    A bare dotted name is legitimately a module for ``test-impact``, so module names win first; only a name that is not
    a module is normalized to a symbol. A name matching nothing falls back to module mode, which reports the historic
    "Module not found" error; an ambiguous name exits with its candidate list.

    Args:
        index: parsed codemap index dict.
        qname: the caller's ``test-impact`` target.
        module_map: name-keyed module lookup from :func:`build_module_map`.
    """
    if "::" in qname:
        return _SymbolTarget(qname, found=True)
    if qname in module_map:
        return None
    resolved = _normalize_symbol_target(index, _get_symbol_map(index), qname)
    if resolved.found:
        return resolved
    if resolved.candidates:
        _exit_symbol_not_found(index, qname, resolved.candidates)
    return None


def _test_impact_via_module_import(index: dict, module_map: dict, qname: str) -> tuple[set[str], set[str]]:
    """BFS the reverse import graph from *qname*; return (test files, test modules) reached.

    Args:
        index: parsed codemap index dict.
        module_map: name-keyed module lookup from :func:`build_module_map`.
        qname: bare dotted module name whose importers are traced transitively.
    """
    if qname not in module_map:
        _exit_target_not_found(f"Module '{qname}' not found in index.", qname)
    rev_imports = _get_rev_import_graph(index)
    test_files: set[str] = set()
    test_mods: set[str] = set()
    visited: set[str] = {qname}
    queue: deque[str] = deque([qname])
    while queue:
        node = queue.popleft()
        for importer in rev_imports.get(node, []):
            if importer not in visited:
                visited.add(importer)
                m_entry = module_map.get(importer, {})
                if m_entry.get("is_test"):
                    if p := m_entry.get("path", ""):
                        test_files.add(p)
                    if n := m_entry.get("name", ""):
                        test_mods.add(n)
                queue.append(importer)
    return test_files, test_mods


def _test_impact_via_mocks(index: dict, qname: str) -> tuple[set[str], set[str]]:
    """Return (test files, test modules) that mock *qname* via ``patch()``, call/import path or not.

    Args:
        index: parsed codemap index dict.
        qname: ``module::symbol`` (exact target) or bare module (prefix match on
            every ``module::symbol`` it owns).
    """
    mock_prefix = f"{qname}::" if "::" not in qname else None
    test_files: set[str] = set()
    test_mods: set[str] = set()
    for m in index.get("modules", []):
        if not m.get("is_test"):
            continue
        for patch in m.get("mock_patches", []) or []:
            target = patch.get("target", "")
            if not target:
                continue
            hit = target == qname or (mock_prefix and target.startswith(mock_prefix))
            if hit:
                p = patch.get("file", m.get("path", ""))
                if p:
                    test_files.add(p)
                if n := m.get("name", ""):
                    test_mods.add(n)
    return test_files, test_mods


def cmd_test_impact(index: dict, qname: str, include_mocks: bool = True) -> None:
    """Which tests are affected by changing *qname*?

    Two input modes:

    * ``module::symbol`` — BFS over static reverse call graph; filters to
      test modules. Also collects ``mock_patches`` for the symbol.
    * bare ``module`` — BFS over static reverse import graph; filters to
      test modules. Also collects ``mock_patches`` for every symbol in the
      module.

    A target that is neither ``module::symbol`` nor an indexed module name is
    normalized like the ``fn-*`` commands (dotted ``module.symbol`` or a unique
    bare/class-qualified name) and then runs in function mode.

    In function mode a method or constructor with no static caller at all also gets
    the ``fn-rdeps`` zero-caller ``hint``; module mode never does.

    Args:
        index: parsed codemap index dict.
        qname: ``module::symbol`` for function-level impact, or bare dotted
            module name for module-level impact.
        include_mocks: when True (default) add test files that mock *qname*
            even if they have no call/import path to it.

    Examples:
        ``mypackage.trainer::Trainer.fit`` — tests calling fit (directly or
        transitively) plus tests that mock Trainer.fit.
        ``mypackage.utils`` — tests that import utils through any chain.
    """
    module_map = build_module_map(index) if "::" not in qname else {}
    target = _test_impact_function_target(index, qname, module_map)
    merged = False
    hint = None
    if target is not None:
        qname = target.qname
        test_files_via_call, test_mods_via_call, merged, has_callers = _test_impact_via_function_call(index, qname)
        # No static caller at all, not merely no test caller: a test reaching the method through an instance
        # (`obj.method()`) is invisible to this walk, so "0 tests" there is unresolved, not proof of no coverage.
        hint = None if has_callers else _zero_caller_hint(_get_symbol_map(index), qname)
    else:
        test_files_via_call, test_mods_via_call = _test_impact_via_module_import(index, module_map, qname)

    test_files_via_mock: set[str] = set()
    test_mods_via_mock: set[str] = set()
    if include_mocks:
        test_files_via_mock, test_mods_via_mock = _test_impact_via_mocks(index, qname)

    all_files = sorted(test_files_via_call | test_files_via_mock)
    all_mods = sorted(test_mods_via_call | test_mods_via_mock)
    pytest_cmd = ("pytest " + " ".join(all_files)) if all_files else ""
    method = "static-ast" if "::" in qname else "import-graph"
    short_name = qname.split("::")[-1] if "::" in qname else qname.split(".")[-1]
    payload: dict = {
        "qname": qname,
        **(_normalized_fields(target) if target is not None else {}),
        "test_files": all_files,
        "test_modules": all_mods,
        "via_call": len(test_files_via_call),
        "via_mock": len(test_files_via_mock),
        "total": len(all_files),
        "pytest_cmd": pytest_cmd,
    }
    if merged:
        payload["constructor_callers_merged"] = True
    if hint:
        payload["hint"] = hint
    payload["index"] = _cmd_coverage(
        index,
        query_target=qname,
        method=method,
        scope="test-impact",
        answer_hint=hint is not None,
        not_covered=_call_graph_not_covered(merged),
        hint=f'grep -rn "{short_name}" tests/ to find hook-based test deps not in static graph',
    )
    _print(json.dumps(payload))


def cmd_mock_rdeps(index: dict, query: str) -> None:
    """Print test files that mock *query* via ``patch``/``mocker.patch``.

    Two modes:
      * ``module::symbol`` — return callers for that one symbol
      * bare ``module`` — return every mocked symbol in the module with its callers

    Args:
        index: parsed codemap index dict (must be v4.1+ with mock_patches).
        query: either ``module::symbol`` or bare ``module`` dotted name.

    Examples:
        ``mypackage.core::MyClass.method`` — caller list for that symbol only.
        ``mypackage.core`` — every mocked symbol in ``mypackage.core``.
    """
    _require_feature(index, MOCK_PATCHES_MIN_VER, "mock_patches")
    by_symbol: dict[str, list[dict]] = {}
    for m in index.get("modules", []):
        if not m.get("is_test"):
            continue
        for entry in m.get("mock_patches", []) or []:
            target = entry.get("target")
            if not target:
                continue
            by_symbol.setdefault(target, []).append(
                {
                    "file": entry.get("file", ""),
                    "line": entry.get("line", 0),
                    "form": entry.get("form", ""),
                }
            )

    if "::" in query:
        module, symbol = query.split("::", 1)
        callers = sorted(by_symbol.get(query, []), key=lambda c: (c["file"], c["line"]))
        _print(
            json.dumps(
                {
                    "module": module,
                    "symbol": symbol,
                    "callers": callers,
                    "count": len(callers),
                    "index": _cmd_coverage(index, method="ast-flags", scope="mock-patch-strings"),
                }
            )
        )
        return

    prefix = f"{query}::"
    callers: list[dict] = []
    for target, hits in by_symbol.items():
        if not target.startswith(prefix):
            continue
        for hit in hits:
            callers.append(
                {
                    "target": target,
                    "file": hit["file"],
                    "line": hit["line"],
                    "form": hit["form"],
                }
            )
    callers.sort(key=lambda c: (c["target"], c["file"], c["line"]))
    _print(
        json.dumps(
            {
                "module": query,
                "callers": callers,
                "count": len(callers),
                "index": _cmd_coverage(index, method="ast-flags", scope="mock-patch-strings"),
            }
        )
    )
