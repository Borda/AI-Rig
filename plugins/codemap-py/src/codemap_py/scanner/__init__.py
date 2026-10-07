"""codemap_py.scanner — file discovery and single-file AST parsing.

Owns everything needed to turn one project root into a flat list of per-file module
entries: directory walking with exclusion rules (``[tool.codemap] exclude``/
``.codemapignore``/built-in ``SKIP_DIRS``), git/MD5 file hashing, source-root detection,
and the AST extraction that produces one module's ``symbols``/``calls``/``docstrings``/
``mock_patches``/``dynamic_imports``/``sphinx_xrefs``/``subprocess_calls``/``fixtures``
records. Cross-module aggregation (reverse-dependency counts, fixture/coverage/doc-xref
graphs, dedup, the top-level ``scan()``/``incremental_scan()`` pipeline) lives in
:mod:`codemap_py.graph`, which imports the extraction primitives defined here.

``bin/scan-index`` is a thin launcher over :func:`codemap_py.graph.main`;
``bin/_exclusions.py`` is a compatibility shim that aliases this module in
``sys.modules`` so ``scan-query`` (which imports the bare ``_exclusions`` name
from its own ``bin/`` ``sys.path`` insert) reaches this one implementation.

consumers: bin/scan-index (via codemap_py.graph), bin/_exclusions.py shim, bin/scan-query (via the shim)
"""

# The pre-split ``scanner.py`` exposed its own module-level imports as attributes, and
# in-tree callers patch through them (``monkeypatch.setattr(scanner.os, "walk", ...)``)
# or read them directly (``scanner.EntityType``). Re-exported here so the package keeps
# byte-for-byte the same attribute surface as the module it replaces.
import ast  # noqa: F401
import builtins  # noqa: F401
import fnmatch  # noqa: F401
import functools  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from pathlib import Path  # noqa: F401

from codemap_py.schema import EntityType, Resolution, SymbolType  # noqa: F401

from .calls import BUILTINS, _extract_class_symbol, _walk_calls, extract_symbols, resolve_call, resolve_call_chain
from .discovery import (
    _DOCS_PATH_RE,
    _EXAMPLES_PATH_RE,
    _GIT_TIMEOUT_S,
    _INIT_NAMES,
    _MAX_FILE_SIZE_BYTES,
    _TEST_PATH_RE,
    _classify_entity,
    _count_loc_and_main_guard,
    _count_py_files,
    _detect_src_root_from_config,
    _detect_src_root_from_init,
    _effective_src_root,
    _git_file_hashes,
    _is_package_dir,
    _is_python_source,
    _iter_python_files,
    _md5_file_hashes,
    _package_src_root,
    count_loc,
    detect_src_root,
    find_root,
    get_file_hashes,
    get_git_sha,
    has_main_guard,
    path_to_module,
)
from .docs_xrefs import (
    _CONFIG_SCAN_PATTERNS,
    _DOTTED_NAME_RE,
    _MKDOCS_BACKTICK_RE,
    _MKDOCS_NAMED_RE,
    _SPHINX_RESOLVABLE_ROLES,
    _SPHINX_XREF_RE,
    _docstring_nodes,
    _iter_doc_files,
    _resolve_mkdocs_identifier,
    _resolve_xref_target,
    extract_sphinx_xrefs,
    scan_config_refs,
    scan_mkdocs_xrefs,
    scan_rst_xrefs,
)
from .exclusions import (
    _GLOB_META_RE,
    INDEXED_PATHSPEC,
    SKIP_DIRS,
    Exclusions,
    _load_exclusions,
    _match_exclusion,
    _parse_codemap_exclude_toml,
    _parse_codemap_src_roots_toml,
    _parse_codemapignore,
    is_excluded,
    load_src_roots,
)
from .imports import (
    _STDLIB_MODULES,
    _drop_top_level_rebindings,
    _extract_imports_and_scope,
    _extract_string_sequence,
    _process_ast_import,
    _process_ast_import_from,
    _resolve_import_from_base,
    _symbol_alias_provenance,
    build_import_scope,
    extract_dynamic_imports,
    extract_imports,
    extract_module_exports,
    extract_module_symbol_alias_limitations,
    extract_module_symbol_aliases,
)
from .mocks import (
    _MOCK_FORM_CALL,
    _MOCK_FORM_DECORATOR,
    _MOCK_FORM_MOCKER,
    _is_patch_call,
    _is_patch_object_call,
    _normalize_patch_target,
    _patch_string_arg,
    _resolve_patch_object,
    extract_mock_patches,
)
from .models import _DOCSTRING_FIRST_LINE_MAX, CallEdge, Symbol, _docstring_fields
from .parse_file import _parse_file, _parse_file_star, _strip_stub_call_edges
from .test_tooling import (
    _PYTEST_BUILTIN_FIXTURES,
    _SUBPROCESS_PY_TOKENS,
    _body_yields,
    _collect_module_aliases,
    _extract_path_file_parent_dir,
    _function_param_names,
    _is_os_system,
    _is_pytest_fixture_decorator,
    _is_python_token,
    _is_subprocess_run_or_popen,
    _is_syspath_insert_call,
    _os_system_script,
    _resolve_path_file_parent_script,
    _resolve_script_to_module,
    _subprocess_script_arg,
    extract_conftest_syspath,
    extract_fixture_uses,
    extract_fixtures,
    extract_subprocess_calls,
)

__all__ = [
    "BUILTINS",
    "INDEXED_PATHSPEC",
    "SKIP_DIRS",
    "_CONFIG_SCAN_PATTERNS",
    "_DOCSTRING_FIRST_LINE_MAX",
    "_DOCS_PATH_RE",
    "_DOTTED_NAME_RE",
    "_EXAMPLES_PATH_RE",
    "_GIT_TIMEOUT_S",
    "_GLOB_META_RE",
    "_INIT_NAMES",
    "_MAX_FILE_SIZE_BYTES",
    "_MKDOCS_BACKTICK_RE",
    "_MKDOCS_NAMED_RE",
    "_MOCK_FORM_CALL",
    "_MOCK_FORM_DECORATOR",
    "_MOCK_FORM_MOCKER",
    "_PYTEST_BUILTIN_FIXTURES",
    "_SPHINX_RESOLVABLE_ROLES",
    "_SPHINX_XREF_RE",
    "_STDLIB_MODULES",
    "_SUBPROCESS_PY_TOKENS",
    "_TEST_PATH_RE",
    "CallEdge",
    "Exclusions",
    "Symbol",
    "_body_yields",
    "_classify_entity",
    "_collect_module_aliases",
    "_count_loc_and_main_guard",
    "_count_py_files",
    "_detect_src_root_from_config",
    "_detect_src_root_from_init",
    "_docstring_fields",
    "_docstring_nodes",
    "_drop_top_level_rebindings",
    "_effective_src_root",
    "_extract_class_symbol",
    "_extract_imports_and_scope",
    "_extract_path_file_parent_dir",
    "_extract_string_sequence",
    "_function_param_names",
    "_git_file_hashes",
    "_is_os_system",
    "_is_package_dir",
    "_is_patch_call",
    "_is_patch_object_call",
    "_is_pytest_fixture_decorator",
    "_is_python_source",
    "_is_python_token",
    "_is_subprocess_run_or_popen",
    "_is_syspath_insert_call",
    "_iter_doc_files",
    "_iter_python_files",
    "_load_exclusions",
    "_match_exclusion",
    "_md5_file_hashes",
    "_normalize_patch_target",
    "_os_system_script",
    "_package_src_root",
    "_parse_codemap_exclude_toml",
    "_parse_codemap_src_roots_toml",
    "_parse_codemapignore",
    "_parse_file",
    "_parse_file_star",
    "_patch_string_arg",
    "_process_ast_import",
    "_process_ast_import_from",
    "_resolve_import_from_base",
    "_resolve_mkdocs_identifier",
    "_resolve_patch_object",
    "_resolve_path_file_parent_script",
    "_resolve_script_to_module",
    "_resolve_xref_target",
    "_strip_stub_call_edges",
    "_subprocess_script_arg",
    "_symbol_alias_provenance",
    "_walk_calls",
    "build_import_scope",
    "count_loc",
    "detect_src_root",
    "extract_conftest_syspath",
    "extract_dynamic_imports",
    "extract_fixture_uses",
    "extract_fixtures",
    "extract_imports",
    "extract_mock_patches",
    "extract_module_exports",
    "extract_module_symbol_alias_limitations",
    "extract_module_symbol_aliases",
    "extract_sphinx_xrefs",
    "extract_subprocess_calls",
    "extract_symbols",
    "find_root",
    "get_file_hashes",
    "get_git_sha",
    "has_main_guard",
    "is_excluded",
    "load_src_roots",
    "path_to_module",
    "resolve_call",
    "resolve_call_chain",
    "scan_config_refs",
    "scan_mkdocs_xrefs",
    "scan_rst_xrefs",
]
