"""codemap_py.query — query the codemap structural index.

``bin/scan-query`` is a thin launcher that imports :func:`main` from this
module; :mod:`codemap_py.cli` calls :func:`main` directly in-process (no
subprocess) under its shared read lease. ``_exclusions`` still resolves
through a bin/-relative ``sys.path`` insert rather than a direct package
import — the one remaining transitional seam.

Commands (module-level):
  deps <module>           What does this module import?
  rdeps <module>          What imports this module?
  central [--top N]       Most-imported modules (highest blast radius)
  coupled [--top N]       Modules with the most imports (highest coupling)
  path <from> <to>        Shortest import path between two modules
  list                    All indexed modules with their file paths
  symbol <name>           Get source of a symbol by name (function/class/method)
  symbols <module>        List all symbols in a module
  find-symbol <pattern>   Regex search across all symbol names

Commands (function-level — requires v3 index with call graph):
  fn-deps <qname>         What does a function call?
  fn-rdeps <qname>        What calls a function?
  fn-central [--top N]    Most-called functions globally
  fn-blast <qname>        Transitive reverse-call blast radius
  test-impact <qname>     Tests affected by changing a function or module

Commands (mock graph — requires v4.1+ index):
  mock-rdeps <query>      Test files that mock a symbol via patch()

Commands (subprocess graph — requires v5.2+ index):
  subprocess-deps <module>   Modules spawned by <module> as a subprocess
  subprocess-rdeps <module>  Modules that spawn <module> as a subprocess

Commands (pytest fixture graph — requires v5.3+ index):
  fixture-rdeps <name>       Test files that use a fixture
  fixture-graph <test-file>  Full fixture dependency tree for a test file

Commands (docstring coverage — requires v4.4+ index):
  undocumented [module]   Public symbols missing a docstring (use --all to scan everything)

Commands (test coverage — requires v4.2+ index):
  uncovered [module]      Public symbols with no test callers and no mocks (--all for everything)

Commands (line coverage — requires v5.4+ index built with --with-coverage):
  coverage <qname>           Coverage % and test node IDs for a specific symbol
  coverage-gap [module]      Symbols below threshold, sorted by gap desc (use --all to scan everything)

Commands (Sphinx / MkDocs xrefs — requires v4.5+ index):
  xrefs <qname>           List doc cross-references targeting a symbol
  xrefs <module> --broken Find xrefs whose target is not a known symbol

Commands (dead-symbol detection — requires v4.6+ index):
  dead-symbols [--min-loc N]  Public symbols with no callers anywhere
  dead-modules                Modules with no external importers

Commands (entity map — requires v5.5+ index):
  packages                 Top-level packages with module/test/docs/example counts

All output is JSON. Staleness is checked on every invocation — warns to
stderr if Python files were committed after the index was built.

Usage:
    scan-query central --top 5
    scan-query coupled --top 5
    scan-query deps mypackage.auth
    scan-query rdeps mypackage.models
    scan-query path mypackage.api mypackage.db
    scan-query symbol authenticate
    scan-query symbols mypackage.auth
    scan-query find-symbol '^Auth.*Handler$'
    scan-query fn-deps 'mypackage.auth::validate_token'
    scan-query fn-rdeps 'mypackage.db::fetch_user'
    scan-query fn-central --top 5
    scan-query fn-blast 'mypackage.auth::validate_token'
"""

# The pre-split ``query.py`` exposed its own module-level imports as attributes, and
# in-tree callers read them off the module (``query.EntityType``, ``query.Path``).
# Re-exported here so the package keeps the same attribute surface as the module it
# replaces; ``state`` is the shared per-invocation state other modules patch through.
import argparse  # noqa: F401
import ast  # noqa: F401
import calendar  # noqa: F401
import csv  # noqa: F401
import io  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import signal  # noqa: F401
import subprocess  # noqa: F401
import sys
import time  # noqa: F401
from collections import deque  # noqa: F401
from collections.abc import Callable, Sequence  # noqa: F401
from enum import Enum  # noqa: F401
from pathlib import Path
from typing import NamedTuple  # noqa: F401

from codemap_py import index_paths, rwgate  # noqa: F401
from codemap_py import query_state as state  # noqa: F401
from codemap_py.scanner import INDEXED_PATHSPEC  # noqa: F401
from codemap_py.schema import (  # noqa: F401
    CALL_GRAPH_MIN_VER,
    COVERAGE_MIN_VER,
    DEAD_SYMBOL_MIN_VER,
    DOCSTRING_MIN_VER,
    FIXTURE_GRAPH_MIN_VER,
    IMPORT_GROUPS_MIN_VER,
    MOCK_PATCHES_MIN_VER,
    MODULE_ALIASES_MIN_VER,
    SPHINX_XREFS_MIN_VER,
    SUBPROCESS_CALLS_MIN_VER,
    UNCOVERED_MIN_VER,
    VALID_CALL_RESOLUTIONS,
    EntityType,
    Symbol,
    validate_index,
)
from codemap_py.telemetry import CliInvocation, runtime_id  # noqa: F401

#: Plugin bin/ directory, added to sys.path so the _exclusions shim can be imported.
_BIN = Path(__file__).resolve().parents[3] / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))
from _exclusions import Exclusions, _load_exclusions, _match_exclusion, is_excluded  # noqa: E402,F401

from .callgraph import (  # noqa: E402
    _test_impact_via_function_call,
    _test_impact_via_mocks,
    _test_impact_via_module_import,
    cmd_fn_blast,
    cmd_fn_central,
    cmd_fn_deps,
    cmd_fn_rdeps,
    cmd_mock_rdeps,
    cmd_test_impact,
)
from .cli import (  # noqa: E402
    _COMMAND_HANDLERS,
    _add_callgraph_subparsers,
    _add_composite_subparsers,
    _add_docs_coverage_subparsers,
    _add_global_flags,
    _add_module_subparsers,
    _add_subprocess_fixture_subparsers,
    _add_symbol_subparsers,
    _add_xref_dead_subparsers,
    _build_parser,
    _dispatch_command,
    _resolve_index_path,
    _run_query,
    _ScanQueryArgumentParser,
    main,
)
from .coverage import (  # noqa: E402
    _CALL_GRAPH_NOT_COVERED,
    _COMPACT_ALIAS_LIMITATION_LIMIT,
    _GLOBAL_IN_DIRECTION_CMDS,
    _IMPORT_GRAPH_NOT_COVERED,
    _LOCAL_DIRECTION_CMDS,
    _SESSION_MARKER_TTL_MS,
    _cmd_coverage,
    _compact_alias_limitations,
    _coverage,
    _coverage_already_emitted,
    _coverage_cache,
    _coverage_full_keys,
    _coverage_note,
    _degraded_relevant,
    _local_complete,
    _query_complete,
    _read_session_marker,
    _should_compact_coverage,
    _target_path_tokens,
    _valid_session_id,
    _wide_complete,
)
from .diff_batch import (  # noqa: E402
    _NON_NESTABLE_IN_BATCH,
    _RISK_HIGH_MIN_RDEPS,
    _batch_item_argv,
    _diff_impact_for_module,
    _diff_impact_tests,
    _git_diff_line_ranges,
    _git_diff_paths,
    _load_batch_items,
    _map_changed_files,
    _parse_unified_diff,
    _risk_tier,
    _run_subquery,
    _symbols_in_ranges,
    cmd_batch,
    cmd_diff_impact,
)
from .docs_coverage import (  # noqa: E402
    UncoveredSort,
    _coverage_measurement,
    _find_module,
    _is_public_symbol,
    _module_coverage_gap_candidates,
    _module_uncovered_candidates,
    _split_coverage_qname,
    _symbol_loc,
    cmd_coverage,
    cmd_coverage_gap,
    cmd_uncovered,
    cmd_undocumented,
)
from .errors import (  # noqa: E402
    _EXIT_BAD_INPUT,
    _EXIT_GENERIC,
    _EXIT_NOT_INDEXED,
    _builtin_print,
    _die_json,
    _die_module_not_indexed,
    _exit_error,
    _exit_symbol_not_found,
)
from .index_io import (  # noqa: E402
    _GIT_TIMEOUT_S,
    _HEAL_MAX_CHANGED_FILES,
    _HEAL_TIMEOUT_S,
    _INDEXED_PATHSPEC,
    _MAX_INDEX_SIZE_BYTES,
    _SELF_CHECK_DETAIL,
    _SHAS_GIT_ERROR,
    _SHAS_NO_REPO,
    _SHAS_OK,
    _,
    _alias_limitations_for_target,
    _autobuild_disabled,
    _build_rev_import_graph_raw,
    _changed_py_files,
    _current_file_shas,
    _current_git_sha,
    _current_sha_cache,
    _current_sha_resolved,
    _detect_root_mismatch,
    _emit_gate_error,
    _exclusions_cache,
    _exclusions_resolved,
    _file_shas_cache,
    _FileShas,
    _find_index_in_scan_dir,
    _find_index_via_cwd_walk,
    _find_index_via_git_root,
    _gate_timeout_kwargs,
    _get_current_file_shas,
    _get_current_sha_cached,
    _get_exclusions_cached,
    _get_git_root_cached,
    _get_rev_graph,
    _get_rev_import_graph,
    _get_symbol_map,
    _git_cwd_kwargs,
    _git_root,
    _git_root_cache,
    _git_root_resolved,
    _has_call_graph,
    _indexed_untracked_modified,
    _is_valid_index_file,
    _load_index_leased,
    _parse_ls_files_stage,
    _require_call_graph,
    _require_feature,
    _require_sphinx_xref_count,
    _require_subprocess_rdep_count,
    _resolve_current_file_shas,
    _resolve_project_root,
    _resolve_symbol_alias,
    _rev_graph_cache,
    _rev_import_graph_cache,
    _run_incremental_scan,
    _safe_glob_candidates,
    _symbol_alias_limitations,
    _symbol_map_cache,
    _untracked_py_files,
    _warn_staleness_undetermined,
    build_module_map,
    build_reverse_call_graph,
    build_symbol_map,
    check_staleness,
    find_index,
    load_index,
    maybe_self_heal,
    warn_if_stale,
)
from .modules import (  # noqa: E402
    _as_entity,
    _as_module_list,
    _entity_type,
    _production_rdep_counts,
    cmd_central,
    cmd_coupled,
    cmd_deps,
    cmd_import_types,
    cmd_list,
    cmd_packages,
    cmd_path,
    cmd_rdeps,
)
from .output import _emit_tsv, _empty_table_key, _print, _tabular_key, _to_tsv  # noqa: E402
from .subprocess_fixtures import (  # noqa: E402
    _FIXTURE_GRAPH_MAX_DEPTH,
    _build_fixture_subtree,
    _collect_fixture_definitions,
    _find_test_module,
    cmd_fixture_graph,
    cmd_fixture_rdeps,
    cmd_subprocess_deps,
    cmd_subprocess_rdeps,
)
from .symbols import (  # noqa: E402
    _ALT_REDOS_RE,
    _DANGEROUS_PATTERN,
    _STALE_CATEGORY,
    _extract_import_block,
    _find_symbol_matches,
    _is_dangerous_regex,
    _reject_multiline_args,
    _scan_symbols,
    _symbol_group_results,
    _symbol_source_and_staleness,
    cmd_find_symbol,
    cmd_symbol,
    cmd_symbols,
)
from .xrefs_dead import (  # noqa: E402
    _SYMBOL_ROLES,
    _dead_symbol_eligible_modules,
    _fn_rdep_count,
    _iter_all_xrefs,
    _module_dead_symbol_candidates,
    cmd_dead_modules,
    cmd_dead_symbols,
    cmd_xrefs,
)

__all__ = [
    "_ALT_REDOS_RE",
    "_CALL_GRAPH_NOT_COVERED",
    "_COMMAND_HANDLERS",
    "_COMPACT_ALIAS_LIMITATION_LIMIT",
    "_DANGEROUS_PATTERN",
    "_EXIT_BAD_INPUT",
    "_EXIT_GENERIC",
    "_EXIT_NOT_INDEXED",
    "_FIXTURE_GRAPH_MAX_DEPTH",
    "_GIT_TIMEOUT_S",
    "_GLOBAL_IN_DIRECTION_CMDS",
    "_HEAL_MAX_CHANGED_FILES",
    "_HEAL_TIMEOUT_S",
    "_IMPORT_GRAPH_NOT_COVERED",
    "_INDEXED_PATHSPEC",
    "_LOCAL_DIRECTION_CMDS",
    "_MAX_INDEX_SIZE_BYTES",
    "_NON_NESTABLE_IN_BATCH",
    "_RISK_HIGH_MIN_RDEPS",
    "_SELF_CHECK_DETAIL",
    "_SESSION_MARKER_TTL_MS",
    "_SHAS_GIT_ERROR",
    "_SHAS_NO_REPO",
    "_SHAS_OK",
    "_STALE_CATEGORY",
    "_SYMBOL_ROLES",
    "UncoveredSort",
    "_",
    "_FileShas",
    "_ScanQueryArgumentParser",
    "_add_callgraph_subparsers",
    "_add_composite_subparsers",
    "_add_docs_coverage_subparsers",
    "_add_global_flags",
    "_add_module_subparsers",
    "_add_subprocess_fixture_subparsers",
    "_add_symbol_subparsers",
    "_add_xref_dead_subparsers",
    "_alias_limitations_for_target",
    "_as_entity",
    "_as_module_list",
    "_autobuild_disabled",
    "_batch_item_argv",
    "_build_fixture_subtree",
    "_build_parser",
    "_build_rev_import_graph_raw",
    "_builtin_print",
    "_changed_py_files",
    "_cmd_coverage",
    "_collect_fixture_definitions",
    "_compact_alias_limitations",
    "_coverage",
    "_coverage_already_emitted",
    "_coverage_cache",
    "_coverage_full_keys",
    "_coverage_measurement",
    "_coverage_note",
    "_current_file_shas",
    "_current_git_sha",
    "_current_sha_cache",
    "_current_sha_resolved",
    "_dead_symbol_eligible_modules",
    "_degraded_relevant",
    "_detect_root_mismatch",
    "_die_json",
    "_die_module_not_indexed",
    "_diff_impact_for_module",
    "_diff_impact_tests",
    "_dispatch_command",
    "_emit_gate_error",
    "_emit_tsv",
    "_empty_table_key",
    "_entity_type",
    "_exclusions_cache",
    "_exclusions_resolved",
    "_exit_error",
    "_exit_symbol_not_found",
    "_extract_import_block",
    "_file_shas_cache",
    "_find_index_in_scan_dir",
    "_find_index_via_cwd_walk",
    "_find_index_via_git_root",
    "_find_module",
    "_find_symbol_matches",
    "_find_test_module",
    "_fn_rdep_count",
    "_gate_timeout_kwargs",
    "_get_current_file_shas",
    "_get_current_sha_cached",
    "_get_exclusions_cached",
    "_get_git_root_cached",
    "_get_rev_graph",
    "_get_rev_import_graph",
    "_get_symbol_map",
    "_git_cwd_kwargs",
    "_git_diff_line_ranges",
    "_git_diff_paths",
    "_git_root",
    "_git_root_cache",
    "_git_root_resolved",
    "_has_call_graph",
    "_indexed_untracked_modified",
    "_is_dangerous_regex",
    "_is_public_symbol",
    "_is_valid_index_file",
    "_iter_all_xrefs",
    "_load_batch_items",
    "_load_index_leased",
    "_local_complete",
    "_map_changed_files",
    "_module_coverage_gap_candidates",
    "_module_dead_symbol_candidates",
    "_module_uncovered_candidates",
    "_parse_ls_files_stage",
    "_parse_unified_diff",
    "_print",
    "_production_rdep_counts",
    "_query_complete",
    "_read_session_marker",
    "_reject_multiline_args",
    "_require_call_graph",
    "_require_feature",
    "_require_sphinx_xref_count",
    "_require_subprocess_rdep_count",
    "_resolve_current_file_shas",
    "_resolve_index_path",
    "_resolve_project_root",
    "_resolve_symbol_alias",
    "_rev_graph_cache",
    "_rev_import_graph_cache",
    "_risk_tier",
    "_run_incremental_scan",
    "_run_query",
    "_run_subquery",
    "_safe_glob_candidates",
    "_scan_symbols",
    "_should_compact_coverage",
    "_split_coverage_qname",
    "_symbol_alias_limitations",
    "_symbol_group_results",
    "_symbol_loc",
    "_symbol_map_cache",
    "_symbol_source_and_staleness",
    "_symbols_in_ranges",
    "_tabular_key",
    "_target_path_tokens",
    "_test_impact_via_function_call",
    "_test_impact_via_mocks",
    "_test_impact_via_module_import",
    "_to_tsv",
    "_untracked_py_files",
    "_valid_session_id",
    "_warn_staleness_undetermined",
    "_wide_complete",
    "build_module_map",
    "build_reverse_call_graph",
    "build_symbol_map",
    "check_staleness",
    "cmd_batch",
    "cmd_central",
    "cmd_coupled",
    "cmd_coverage",
    "cmd_coverage_gap",
    "cmd_dead_modules",
    "cmd_dead_symbols",
    "cmd_deps",
    "cmd_diff_impact",
    "cmd_find_symbol",
    "cmd_fixture_graph",
    "cmd_fixture_rdeps",
    "cmd_fn_blast",
    "cmd_fn_central",
    "cmd_fn_deps",
    "cmd_fn_rdeps",
    "cmd_import_types",
    "cmd_list",
    "cmd_mock_rdeps",
    "cmd_packages",
    "cmd_path",
    "cmd_rdeps",
    "cmd_subprocess_deps",
    "cmd_subprocess_rdeps",
    "cmd_symbol",
    "cmd_symbols",
    "cmd_test_impact",
    "cmd_uncovered",
    "cmd_undocumented",
    "cmd_xrefs",
    "find_index",
    "load_index",
    "main",
    "maybe_self_heal",
    "warn_if_stale",
]
