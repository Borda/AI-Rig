"""Turn one source file into its index entry, orchestrating every other extractor."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from . import discovery
from .calls import extract_symbols
from .discovery import (
    _TEST_PATH_RE,
    _classify_entity,
    _count_loc_and_main_guard,
    _package_src_root,
    path_to_module,
)
from .docs_xrefs import extract_sphinx_xrefs
from .imports import (
    _extract_imports_and_scope,
    _symbol_alias_provenance,
    extract_dynamic_imports,
    extract_imports,
    extract_module_exports,
)
from .mocks import extract_mock_patches


def _parse_file_star(args: tuple[Path, Path, Path]) -> dict:
    """Unpack args tuple and delegate to _parse_file (required for ProcessPoolExecutor.map)."""
    return _parse_file(*args)


def _strip_stub_call_edges(symbol_dicts: list[dict]) -> None:
    """Clear outgoing call edges from a ``.pyi`` stub's symbols.

    A stub declares signatures with ``...`` bodies, so it contributes declarations and
    imports but no executable-body call edges. Empty bodies yield none anyway; this also
    drops any module-level call in a stub (e.g. ``T = TypeVar("T")``).
    """
    for sym in symbol_dicts:
        if sym.get("calls"):
            sym["calls"] = []


def _parse_file(filepath: Path, root: Path, src_root: Path) -> dict:
    """Parse a single .py/.pyi file and return its module entry dict (ok or degraded).

    Args:
        filepath: absolute path to the .py file to parse.
        root: project root used to compute relative paths.
        src_root: source root used to derive the dotted module name.
    """
    rel_path = filepath.relative_to(root)
    try:
        name = path_to_module(filepath, src_root)
    except ValueError:
        name = path_to_module(filepath, _package_src_root(filepath, root))

    file_size = filepath.stat().st_size
    # Read through the module, not a bound local: tests override the cap with
    # monkeypatch.setattr(scanner.discovery, "_MAX_FILE_SIZE_BYTES", ...), which a
    # ``from .discovery import`` binding here would not see.
    if file_size > discovery._MAX_FILE_SIZE_BYTES:
        print(
            f"[codemap] ⚠ skipping {rel_path}: file too large ({file_size // (1024 * 1024)} MB > "
            f"{discovery._MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB limit)",
            file=sys.stderr,
        )
        return {
            "name": name,
            "path": rel_path.as_posix(),
            "status": "degraded",
            "reason": f"file too large ({file_size} bytes) — skipped to prevent OOM",
        }
    try:
        try:
            source = filepath.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            # errors="replace" would substitute U+FFFD and index corrupted source
            # silently; mark degraded so scan-query surfaces it in degraded_files.
            return {"name": name, "path": rel_path.as_posix(), "status": "degraded", "reason": f"encoding: {exc}"}
        tree = ast.parse(source, filename=str(filepath))
        try:
            imports, submodule_candidates, nm, mm, star_imports = _extract_imports_and_scope(
                tree, name, filepath.name in {"__init__.py", "__init__.pyi"}
            )
        except Exception as exc:
            print(f"[codemap] ⚠ import scope build failed for {rel_path}: {exc}", file=sys.stderr)
            imports = extract_imports(tree)
            submodule_candidates, nm, mm, star_imports = [], {}, {}, []
        symbols = extract_symbols(tree, name, nm, mm, star_imports or None)
        dynamic_imports = extract_dynamic_imports(tree)
        _loc, _is_entry = _count_loc_and_main_guard(source)
        is_test = bool(_TEST_PATH_RE.search(rel_path.as_posix()))
        entity_type, pkg = _classify_entity(rel_path, name)
        sphinx_xrefs = extract_sphinx_xrefs(tree, filepath, root, name)
        exports = extract_module_exports(tree)
        symbol_aliases, symbol_alias_limitations = _symbol_alias_provenance(
            tree, name, filepath.name in {"__init__.py", "__init__.pyi"}
        )
        symbol_dicts = [s.as_dict() for s in symbols]
        if filepath.suffix == ".pyi":
            _strip_stub_call_edges(symbol_dicts)
        entry: dict = {
            "name": name,
            "path": rel_path.as_posix(),
            "loc": _loc,
            "dep_count": len(imports),
            "direct_imports": imports,
            "unresolved_direct_imports": imports,
            "from_import_submodules": submodule_candidates,
            "symbols": symbol_dicts,
            "is_entry_point": _is_entry,
            "is_test": is_test,
            "entity_type": entity_type,
            "package": pkg,
            "has_star_imports": bool(star_imports),
            "exports": exports,
            "symbol_aliases": symbol_aliases,
            "symbol_alias_limitations": symbol_alias_limitations,
            "sphinx_xrefs": sphinx_xrefs,
            "status": "ok",
        }
        if dynamic_imports:
            entry["dynamic_imports"] = dynamic_imports
        if is_test:
            entry["mock_patches"] = extract_mock_patches(tree, filepath)
        return entry
    except SyntaxError as exc:
        return {"name": name, "path": rel_path.as_posix(), "status": "degraded", "reason": f"SyntaxError: {exc}"}
    except Exception as exc:
        return {"name": name, "path": rel_path.as_posix(), "status": "degraded", "reason": str(exc)}
