"""Index discovery, token counting, MCP probing, and repository import scanning."""

import ast
import subprocess
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from _bench_common.agentic_contracts import AgenticOracle, AnswerScore  # noqa: F401
from _bench_common.claude_stages import (  # noqa: F401
    _FIX_MULTI_QUERY_ARGUMENTS,
    _FIX_SINGLE_QUERY_ARGUMENTS,
    _PATCH_QUERY_ARGUMENTS,
    _READCROP_ANSWER_RE,
    FIX_MULTI_TASKS_PATH,
    FIX_SINGLE_ARMS,
    FIX_SINGLE_TASKS_PATH,
    PARITY_MANIFEST_PATH,
    PATCH_TASKS_PATH,
    READCROP_ARMS,
    READCROP_TASKS_PATH,
    FixMultiContract,
    FixSingleContract,
    PurePosixPath,
    ReadcropUsage,
    StageIdentity,
    _absolute_codemap_launchers,
    _claude_codemap_evidence,
    _claude_event_summary,
    _claude_message_blocks,
    _command_arguments,
    _compact_query_result_succeeded,
    _frozen_index_recovery_attempted,
    _is_compact_query,
    _is_inside_workspace,
    _load_claude_fix_tasks,
    _manifest_sha256,
    _native_tool_result_succeeded,
    _outside_workspace_path_evidence,
    _patch_index_path,
    _patch_stage_identity,
    _provider_binding,
    _query_arguments_from_bash,
    _query_command_tail,
    _readcrop_module_path,
    _resolve_claude_fix_scope,
    _study_query_arguments,
    _tool_input_strings,
    _tool_result_text,
    _workspace_containment_roots,
    build_edit_task_contract,
    build_fix_multi_contract,
    build_fix_single_contract,
    build_readcrop_contract,
    extract_readcrop_symbol_source,
    load_claude_fix_multi_tasks,
    load_claude_fix_single_tasks,
    load_claude_patch_tasks,
    load_claude_readcrop_tasks,
    parse_claude_readcrop_events,
    parse_readcrop_answer,
    prompt_hash,
    readcrop_prompt,
    resolve_claude_fix_multi_scope,
    resolve_claude_fix_single_scope,
    resolve_claude_patch_scope,
    resolve_readcrop_scope,
    score_readcrop_answer,
    stage_contract_sha256,
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.
from _bench_common.codemap_discovery import resolve_index_path

# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.python_source import (
    extract_import_targets,
    iter_py_files,
    module_from_init_chain,
    resolve_relative_base,  # noqa: F401
)


def count_tokens(text: str) -> int:
    """Approximate token count using tiktoken o200k_base (matches caveman evals)."""
    try:
        import tiktoken

        enc = tiktoken.get_encoding("o200k_base")
        return len(enc.encode(text))
    except ImportError:
        return max(1, len(text) // 4)  # ~4 chars/token fallback


def find_index(repo_path: Path, explicit: Path | None) -> Path:
    """Locate the pre-built codemap index for the target repo.

    Thin adapter over :func:`_bench_common.codemap_discovery.resolve_index_path`: exact ``<repo_name>.json``
    match (no ``-master``/``-main`` stripping), ``.cache/codemap/`` before ``.cache/scan/``,
    resolved paths, and a raise on miss. The index is built once by ``scan-index`` and
    excluded from benchmark timing; this only validates it exists before any run starts.

    Args:
        repo_path: Root of the repository to benchmark.
        explicit: Caller-supplied index path; returned resolved when provided.

    Returns:
        Resolved absolute path to the located index file.

    Raises:
        FileNotFoundError: If no index is found under ``.cache/codemap/`` or ``.cache/scan/``
            and ``explicit`` was not provided.
    """
    return resolve_index_path(repo_path, explicit, strip_suffixes=False, missing="raise")


def check_semble_mcp() -> None:
    """Verify semble is installed and configured as an MCP server before the run starts.

    Checks two things:
      1. The semble Python package is importable (the MCP server ships with it).
      2. `claude mcp get semble` exits 0 — works for all scopes (user / project / local).

    Raises RuntimeError with actionable instructions when either check fails.
    """
    try:
        import semble as _semble  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "semble package not installed — required for the semble arm.\n"
            "Install it:\n"
            "  pip install semble>=0.1.0\n"
            "  # or: uv add semble"
        )

    r = subprocess.run(
        ["claude", "mcp", "get", "semble"],  # noqa: S607 - git/tool resolved via PATH on purpose
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            "semble MCP server not configured — run once to register it:\n"
            "  claude mcp add semble -s user -- uvx --from 'semble[mcp]' semble\n"
            "Use -s project instead of -s user to scope to this repo only."
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
    """Return a short human-readable argument string for tool call logging.

    >>> _tool_key_arg("Grep", {"pattern": "import auth", "path": "src/"})
    "'import auth' in src/"
    >>> _tool_key_arg("mcp__semble__search", {"query": "import checkpoint_connector", "repo": "repo", "top_k": 20})
    "query='import checkpoint_connector'"
    >>> _tool_key_arg("mcp__semble__find_related", {"query": "find related", "line": 42})
    "query='find related'"
    """
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
    if name in ("mcp__semble__search", "mcp__semble__find_related"):
        inp_q = inp.get("query", "")[:80]
        return f"query={inp_q!r}"
    return str(inp)[:80]


# ---------------------------------------------------------------------------
# Independent AST-based reverse-dependency scan (tool-independent ground truth)
# ---------------------------------------------------------------------------

_SKIP_DIR_PARTS = frozenset({"tests", "test"})  # top-level test trees excluded from production rdeps


def _iter_py_files(root: Path) -> Iterator[Path]:
    """Yield ``*.py`` files under ``root``, pruning hidden and test directories.

    Directories whose name starts with ``.`` (``.git``, ``.cache``, ``.venv``) and any
    directory named ``tests``/``test`` are skipped — the latter mirrors the index rule that
    blast-radius analysis targets production callers only.

    Args:
        root: Repository root to walk.

    Yields:
        Absolute paths to candidate Python source files.
    """
    yield from iter_py_files(root, skip=_SKIP_DIR_PARTS)


def _derive_module_name(py_path: Path, root: Path) -> str | None:
    """Derive the dotted module name of a file in scan-index's namespace.

    A file inside a package (its parent holds an ``__init__.py``) is named via its ``__init__.py``
    chain (:func:`module_from_init_chain`, scan-index Strategy 2). A loose file with no package parent
    is named by its path relative to *root* (separators → dots, ``.py`` dropped) — the same way
    scan-index (``path_to_module``) names a file outside any ``__init__`` chain
    (``examples.pytorch.domain_templates.imagenet``, not the bare stem ``imagenet``). Aligning the two
    keeps the AST oracle and the scan-index oracle in one namespace, so a loose importer no longer
    fires both ``missing_in_index`` and ``missing_in_ast`` as a spurious ``gt-divergence``.

    Args:
        py_path: Absolute path to a ``.py`` file inside ``root``.
        root: Scan root the loose-file dotted name is taken relative to.

    Returns:
        Dotted module name (e.g. ``lightning.pytorch.trainer.trainer`` for an in-package file,
        ``examples.pytorch.domain_templates.imagenet`` for a loose file). ``None`` when no name can
        be derived.

    Examples:
        >>> import tempfile, pathlib
        >>> with tempfile.TemporaryDirectory() as d:
        ...     r = pathlib.Path(d)
        ...     _ = (r / "pkg").mkdir()
        ...     _ = (r / "pkg" / "__init__.py").write_text("")
        ...     _ = (r / "pkg" / "mod.py").write_text("")
        ...     _ = (r / "examples").mkdir()
        ...     _ = (r / "examples" / "demo.py").write_text("")
        ...     (
        ...         _derive_module_name(r / "pkg" / "mod.py", r),
        ...         _derive_module_name(r / "examples" / "demo.py", r),
        ...     )
        ('pkg.mod', 'examples.demo')
    """
    # In-package file: name via the __init__ chain (scan-index Strategy 2), unchanged.
    if (py_path.parent / "__init__.py").exists():
        return module_from_init_chain(py_path) or None
    # Loose file (no package parent): name relative to the scan root — matches how scan-index names
    # files outside any __init__ chain, so both oracles share one namespace instead of the bare stem.
    try:
        rel_dotted = ".".join(py_path.relative_to(root).with_suffix("").parts)
    except ValueError:
        return module_from_init_chain(py_path) or None
    return rel_dotted or None


# resolve_relative_base comes from python_source (shared with run-codemap-cli).


def _extract_import_targets(tree: ast.Module, package: str, all_modules: set[str]) -> set[str]:
    """Collect internal modules a parsed file imports (base and submodule forms).

    Handles ``import a.b.c``, ``import a.b as z``, ``from a.b import c`` (crediting both the
    package ``a.b`` and the submodule ``a.b.c`` when the latter is a real module), and relative
    imports resolved against ``package``. Only targets present in ``all_modules`` are returned,
    so external and symbol-only imports are dropped.

    Args:
        tree: Parsed module AST.
        package: Dotted package of the importing module (for relative resolution).
        all_modules: Set of internal dotted module names to filter targets against.

    Returns:
        Set of internal dotted module names the file depends on.
    """
    # keep=all_modules filters to internal modules; default resolve+credit matches this lane.
    return extract_import_targets(tree, package=package, keep=all_modules)


def _scan_repo_importers(root: Path) -> dict[str, set[str]]:
    """Build a tool-independent reverse-dependency map by AST-parsing every source file once.

    The walk resolves each production module's imports and inverts them into
    ``{imported_module: {importer_module, ...}}``. This is the ground-truth source for quality
    scoring: unlike the codemap index it is not the artefact the codemap arm queries,
    so index blind spots (e.g. ``from pkg import submodule``) surface as divergences instead of
    being invisible.

    Args:
        root: Repository root to scan.

    Returns:
        Mapping from each internal dotted module name to the set of production modules importing it.
    """
    file_module: dict[Path, str] = {}
    for py_file in _iter_py_files(root):
        name = _derive_module_name(py_file, root)
        if name:
            file_module[py_file] = name
    all_modules = set(file_module.values())
    importers: dict[str, set[str]] = defaultdict(set)
    for py_file, module in file_module.items():
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="ignore"), filename=str(py_file))
        except (SyntaxError, ValueError):
            continue
        package = module if py_file.stem == "__init__" else module.rpartition(".")[0]
        for target in _extract_import_targets(tree, package, all_modules):
            if target != module:
                importers[target].add(module)
    return importers
