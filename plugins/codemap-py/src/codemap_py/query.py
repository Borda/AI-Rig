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

from __future__ import annotations

import argparse
import ast
import calendar
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Sequence
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from codemap_py import index_paths, rwgate
from codemap_py.scanner import INDEXED_PATHSPEC

# Transitional seam: exclusion rules live in codemap_py.scanner, but this
# module still reaches them through the old bare-name ``_exclusions`` import
# (bin/_exclusions.py, itself a shim onto codemap_py.scanner) via a
# bin/-relative sys.path insert, the same route bin/scan-index used to take.
# Every other import below is a direct package-internal import.
_BIN = Path(__file__).resolve().parents[2] / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))
from _exclusions import Exclusions, _load_exclusions, _match_exclusion, is_excluded  # noqa: E402

from codemap_py.schema import (  # noqa: E402
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
from codemap_py.telemetry import log_cli, runtime_id  # noqa: E402

# v5.1: MODULE_ALIASES_MIN_VER is imported for downstream feature gating; no
# command consumes it directly today — module_aliases is applied internally by
# scan-index at import resolution time. Touch reference to avoid F401 churn.
_ = MODULE_ALIASES_MIN_VER

# Blind spots disclosed in every import-graph result's ``not_covered`` field.
# Relative imports and known ``from package import submodule`` edges are resolved
# during scanning; dynamic import forms remain outside the static import graph.
_IMPORT_GRAPH_NOT_COVERED = [
    "importlib.import_module",
    "__import__",
    "lazy-loading",
]

# Blind spots disclosed in every static call-graph result's ``not_covered``
# field. Relative import aliases are resolved during scope construction.
_CALL_GRAPH_NOT_COVERED = [
    "dynamic-dispatch",
    "hook-callbacks",
    "string-dispatch",
]


# ---------------------------------------------------------------------------
# Telemetry — cli.jsonl logging
# ---------------------------------------------------------------------------

_T0: float = time.time()
_CMD: str = ""
# No module-level _LOG_DIR: it was a CWD-relative constant frozen at IMPORT time, so a
# query launched from a subdirectory logged to <subdir>/.cache/codemap/logs while the
# hooks logged to <repo-root>/.cache/codemap/logs — one session split across two
# directories, and a CODEMAP_LOG_DIR exported after import was never seen at all.
# log_cli() resolves the project-anchored root itself, per call.
_builtin_print = print  # saved before print( → _print( sweep below

# batch mode: when a list is installed here, _print captures each command's stdout
# JSON into it instead of writing to the real stdout, so cmd_* functions (which all
# emit via _print) can be reused unchanged and their results collected per item.
# stderr writes (file=... kwarg) always pass through untouched.
_capture: list[str] | None = None


def _print(*args: object, **kwargs: object) -> None:
    """Forward to built-in print; for stdout output also append a cli.jsonl record.

    In batch mode (:data:`_capture` set) a stdout write is diverted into the capture buffer and neither printed nor
    logged — the batch driver owns the single real stdout write and one telemetry record for the whole batch. stderr
    writes (``file=`` kwarg) always go straight through regardless of mode.
    """
    if not kwargs.get("file") and _capture is not None:
        _capture.append(str(args[0]) if args else "")
        return
    _builtin_print(*args, **kwargs)
    if kwargs.get("file"):
        return
    raw = str(args[0]) if args else ""
    try:
        result = json.loads(raw)
    except Exception:  # noqa: BLE001 — non-JSON stdout still logs an empty result
        result = {}
    # _CMD empty ⇒ main() never parsed a command: this process imported scan-query
    # (pytest, tooling) and called a cmd_* directly. Logging here stamps the host
    # process argv + import-age timing into cli.jsonl — 4.5K polluted records in
    # the 2026-07 usage audit. Real CLI runs always pass through main() first.
    if _CMD:
        log_cli(_CMD, sys.argv[1:], result, _T0)


def _has_call_graph(index: dict) -> bool:
    """Return True if the index was built with call graph data (schema v3+).

    Gates on the fixed ``CALL_GRAPH_MIN_VER`` floor (3 — when the ``calls`` field first shipped), NOT the live
    ``SCAN_VERSION``: any index at or above v3 carries call edges, so a future ``SCAN_VERSION`` bump must not
    retroactively reject a still-valid pre-current index for fn-deps/fn-rdeps/fn-central/fn-blast.

    Accepts both int and string values for ``scan_version`` — older index files may have been written by tools that
    serialised the field as a string.
    """
    raw = index.get("scan_version", 0)
    try:
        return int(raw) >= CALL_GRAPH_MIN_VER
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Cached git state (populated once in main(), reused across commands)
# ---------------------------------------------------------------------------

_git_root_cache: Path | None = None
_git_root_resolved: bool = False
_current_sha_cache: str | None = None
_current_sha_resolved: bool = False


def _get_git_root_cached() -> Path | None:
    """Return cached git root, resolving on first call."""
    global _git_root_cache, _git_root_resolved
    if not _git_root_resolved:
        _git_root_cache = _git_root()
        _git_root_resolved = True
    return _git_root_cache


def _get_current_sha_cached() -> str | None:
    """Return cached HEAD SHA, resolving on first call."""
    global _current_sha_cache, _current_sha_resolved
    if not _current_sha_resolved:
        _current_sha_cache = _current_git_sha()
        _current_sha_resolved = True
    return _current_sha_cache


_exclusions_cache: Exclusions | None = None
_exclusions_resolved: bool = False


def _get_exclusions_cached() -> Exclusions:
    """Return the project's index exclusions, resolving from git root / CWD once.

    F4: scan-index drops SKIP_DIRS + pyproject/.codemapignore paths from the index.
    scan-query's staleness diff must apply the SAME rules, or a tracked-but-excluded
    ``.py`` (e.g. a vendored tree) re-lists as "added" and forces permanent stale.
    Resolves the root the way scan-index's ``find_root`` does (git root, else CWD) so
    both read the same config files.
    """
    global _exclusions_cache, _exclusions_resolved
    if not _exclusions_resolved:
        root = _get_git_root_cached() or Path.cwd()
        _exclusions_cache = _load_exclusions(root)
        _exclusions_resolved = True
    return _exclusions_cache


# ---------------------------------------------------------------------------
# Index location
# ---------------------------------------------------------------------------


def _git_root() -> Path | None:
    """Return the git repository root, or None if not inside a git repo."""
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
        ).strip()
        return Path(root)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _find_index_via_git_root(git_root: Path) -> Path | None:
    """Strategy 1: return the index under *git_root*'s ``.cache/{codemap,scan}/``, or None."""
    for cache_dir in ("codemap", "scan"):
        candidate = git_root / ".cache" / cache_dir / f"{git_root.name}.json"
        if not candidate.exists():
            continue
        cache_base = git_root / ".cache" / cache_dir
        try:
            candidate.resolve().relative_to(cache_base.resolve())
        except ValueError:
            _print(f"⚠ codemap: skipped {candidate} — resolves outside expected cache dir", file=sys.stderr)
            continue
        return candidate
    return None


def _safe_glob_candidates(scan_dir: Path, candidates: list[Path]) -> list[Path]:
    """Filter *candidates* to those that resolve inside *scan_dir* (guards against symlink escape)."""
    safe = []
    for c in candidates:
        try:
            c.resolve().relative_to(scan_dir.resolve())
            safe.append(c)
        except ValueError:
            _print(f"⚠ codemap: skipped {c} — resolves outside expected cache dir", file=sys.stderr)
    return safe


def _find_index_in_scan_dir(scan_dir: Path, parent: Path) -> Path | None:
    """Return the preferred or best-glob index file under one ``.cache/{codemap,scan}/`` dir, or None.

    Args:
        scan_dir: the ``.cache/codemap`` or ``.cache/scan`` directory to search.
        parent: the directory *scan_dir* lives under, whose name is the preferred stem.
    """
    if not scan_dir.is_dir():
        return None
    preferred = scan_dir / f"{parent.name}.json"
    if preferred.exists():
        try:
            preferred.resolve().relative_to(scan_dir.resolve())
        except ValueError:
            _print(f"⚠ codemap: skipped {preferred} — resolves outside expected cache dir", file=sys.stderr)
            return None
        return preferred
    safe_candidates = _safe_glob_candidates(scan_dir, sorted(scan_dir.glob("*.json")))
    if not safe_candidates:
        return None
    # Schema-validate glob candidates before trusting one: an
    # arbitrary .json in the cache dir must not be loaded unvalidated.
    valid = [c for c in safe_candidates if _is_valid_index_file(c)]
    if not valid:
        _exit_error(
            f"No valid codemap index in {scan_dir} — "
            f"{len(safe_candidates)} .json file(s) present but none match the index schema. "
            "Re-run /codemap-py:scan-codebase to rebuild."
        )
    if len(valid) > 1 or valid[0].stem != parent.name:
        _print(f"⚠ codemap: loaded {valid[0].name} via fallback; expected {parent.name}.json", file=sys.stderr)
    return valid[0]


def _find_index_via_cwd_walk() -> Path | None:
    """Strategy 2: walk up from CWD looking for a ``.cache/{codemap,scan}/`` index (ZIP exports, non-git repos)."""
    for parent in [Path.cwd(), *Path.cwd().parents]:
        for cache_dir in ("codemap", "scan"):
            found = _find_index_in_scan_dir(parent / ".cache" / cache_dir, parent)
            if found is not None:
                return found
    return None


def find_index() -> Path:
    """Locate the codemap index using a multi-strategy search.

    0. ``CODEMAP_INDEX_DIR`` override — the flat ``<override>/<project>.json`` path
       from :func:`codemap_py.index_paths.resolve_index`, the one resolver the index
       writer (:func:`codemap_py.graph.main`), the RW gate, and ``codemap-py doctor``
       also use. Deriving it here independently is what let reader and writer
       normalize the project root differently and disagree on the file name.
    1. Git root — checks .cache/codemap/ then .cache/scan/ (backward compat)
    2. Walk up from CWD — finds index in ZIP exports and non-git repos
    3. Fallback — CWD convention; produces a clear error if missing

    The override path is returned unconditionally (even when the file does not exist
    yet) so a missing index surfaces a clear error at the writer's path instead of
    silently falling back to a stale index under a different convention.
    """
    identity = index_paths.resolve_index()
    if identity.override:
        return identity.index_path

    git_root = _get_git_root_cached()
    if git_root:
        found = _find_index_via_git_root(git_root)
        if found is not None:
            return found

    found = _find_index_via_cwd_walk()
    if found is not None:
        return found

    # Strategy 3: fallback (will surface a clear "Index not found" error)
    root = git_root or Path.cwd()
    return root / ".cache" / "codemap" / f"{root.name}.json"


# 512 MB — guard against a bloated or malicious index causing OOM. This is the ONE
# ceiling: bin/check-index-currency, bin/scan-stats.py and bin/smoke_test_index.py all
# hold the same number rather than a tighter one of their own. The divergence they used
# to carry (50 MB) was not a deliberate second policy — it silently refused real
# indexes. Measured on this repository at 131 MB: the currency probe answered
# ``no_index``, which reads as "no index exists", so the staleness gate stopped firing
# on precisely the large repositories it was written for. A helper cap may never be
# tighter than what the engine will serve; ``TestIndexSizeCapAgreement`` pins that.
_MAX_INDEX_SIZE_BYTES = 512 * 1024 * 1024


def _is_valid_index_file(path: Path) -> bool:
    """Return True if *path* parses as JSON with a minimal codemap index schema.

    Validates the two structural invariants every index must satisfy before it is
    trusted: an integer ``scan_version`` and a list ``modules`` field. Used to
    reject crafted or unrelated ``*.json`` files picked up by the strategy-2 glob
    fallback, which would otherwise be handed to ``load_index`` blindly.

    Size is bounded by ``_MAX_INDEX_SIZE_BYTES`` before parsing to avoid memory
    exhaustion via ``json.load`` on an oversized candidate. Any read/parse error
    is treated as "not a valid index" — the caller moves on to the next candidate.

    Args:
        path: filesystem path to a candidate JSON file.
    """
    try:
        if path.stat().st_size > _MAX_INDEX_SIZE_BYTES:
            return False
        with path.open() as f:
            index = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(index, dict):
        return False
    if not isinstance(index.get("scan_version"), int):
        return False
    if not isinstance(index.get("modules"), list):
        return False
    return True


# Human-readable cause per validate_index() slug — shown on stderr and in the JSON
# error so a caller sees WHY the index was rejected, not just that it was. Every
# message ends in the same rebuild instruction: the fix for a broken index is always
# to re-scan.
_SELF_CHECK_DETAIL = {
    "not_object": "the index root is not a JSON object",
    "missing_keys": "the index is missing required keys (scan_version, modules)",
    "bad_version": "the index scan_version is not an integer",
    "version_too_old": "the index predates the readable schema and cannot be loaded",
    "modules_not_list": "the index 'modules' field is not a list",
    "collisions_not_list": "the index 'collisions' field is corrupt (not a list of objects)",
}


def load_index(path: Path) -> dict:
    """Load, self-check, and return the JSON index from path; exit clearly on any failure.

    After parsing, the decoded object is run through :func:`validate_index` — a
    structural self-check of schema version, required keys, and ``collisions`` sanity.
    A truncated write, a hand-edited file, or an index from an incompatible tool is
    rejected here (stderr warning plus a parseable JSON error advising a re-scan)
    rather than partly served: a command reading a half-valid index returns silently
    wrong answers, which is worse than a hard, actionable failure.

    Args:
        path: filesystem path to the index JSON file.
    """
    if not path.exists():
        if _autobuild_disabled():
            _die_json(
                {
                    "error": "Index not found while SCAN_NO_AUTOBUILD=1 requires an existing frozen index.",
                    "path": str(path),
                    "fix": "Run /codemap-py:scan-codebase before querying, then retry.",
                },
                _EXIT_NOT_INDEXED,
            )
        _exit_error(f"Index not found at {path}. Run /codemap-py:scan-codebase first.")
    index_size = path.stat().st_size
    if index_size > _MAX_INDEX_SIZE_BYTES:
        _exit_error(
            f"Index file too large ({index_size // (1024 * 1024)} MB > "
            f"{_MAX_INDEX_SIZE_BYTES // (1024 * 1024)} MB limit). "
            "Re-run /codemap-py:scan-codebase to rebuild."
        )
    try:
        with path.open() as f:
            index = json.load(f)
    except ValueError as exc:
        # Corrupt/truncated JSON — surface the same rebuild path as a schema failure.
        _print(f"⚠ codemap: index at {path} is not valid JSON: {exc}", file=sys.stderr)
        _die_json(
            {"error": "index is not valid JSON", "path": str(path), "detail": str(exc)},
            _EXIT_NOT_INDEXED,
        )
    reason = validate_index(index)
    if reason is not None:
        detail = _SELF_CHECK_DETAIL.get(reason, "the index failed its structural self-check")
        _print(
            f"⚠ codemap: index at {path} failed self-check ({reason}) — {detail}. "
            "Re-run /codemap-py:scan-codebase to rebuild.",
            file=sys.stderr,
        )
        _die_json(
            {
                "error": "index failed self-check",
                "reason": reason,
                "detail": detail,
                "path": str(path),
                "fix": "Re-run /codemap-py:scan-codebase to rebuild.",
            },
            _EXIT_NOT_INDEXED,
        )
    return index


def _emit_gate_error(code: str, detail: str) -> None:
    """Write one bounded structured RW-gate error to stderr and exit 1.

    Deliberately stderr, and deliberately not :func:`_die_json` (which writes the
    query-level error to stdout): a gate refusal is not a query result, and the
    ``{"error", "detail"}`` shape on stderr is the contract callers already parse
    for ``index_busy``.

    Args:
        code: stable machine-readable slug (e.g. ``index_busy``).
        detail: human-readable one-line cause.
    """
    sys.stderr.write(json.dumps({"error": code, "detail": detail}) + "\n")
    sys.exit(1)


def _gate_timeout_kwargs() -> dict[str, float]:
    """Return the optional bounded gate timeout.

    Absent or non-positive leaves the gate's own default bound; a positive value lets callers (and tests) shorten the
    wait before ``index_busy``.
    """
    raw = os.environ.get("CODEMAP_GATE_TIMEOUT", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return {}
    return {"timeout": value} if value > 0 else {}


#: Path of the index this process actually opened, captured at load time and emitted
#: as ``index.index_path``. Deliberately NOT recomputed from the resolver when emitted:
#: a consumer comparing its own probe path against a resolver-derived answer compares
#: two runs of the same function and learns nothing. This value is the only one that
#: CAN disagree with the resolver — a stale ``CODEMAP_INDEX_DIR`` in the querying
#: process, a different git root, a self-heal that rewrote elsewhere — which is exactly
#: what makes it worth reporting. Empty until a load succeeds; the key is then omitted.
_LOADED_INDEX_PATH: str = ""


def _load_index_leased(index_path: Path) -> dict:
    """Load and self-check the index under a shared read lease.

    The lease is taken HERE, in the engine, rather than by whatever launched it, so
    every route into a query holds one: ``codemap-py query``, ``bin/scan-query``, and
    any in-process caller. It is also scoped to the load alone and released on
    return — a query must never still hold a reader token when it spawns the
    self-heal writer, which would deadlock the child against its own parent until
    the child's deadline expired.

    Args:
        index_path: resolved path of the index to load.

    Returns:
        The parsed, structurally self-checked index dict.
    """
    global _LOADED_INDEX_PATH
    try:
        with rwgate.read_lease(index_path, **_gate_timeout_kwargs()):
            index = load_index(index_path)
        _LOADED_INDEX_PATH = str(index_path)
        return index
    except rwgate.IndexBusy:
        _emit_gate_error("index_busy", "read lease timed out under a live writer")
    except rwgate.IndexUnreadable as exc:
        # Subclass of CoordinationUnavailable — must be caught ahead of it.
        _emit_gate_error("index_unreadable", str(exc))
    except rwgate.CoordinationUnavailable as exc:
        _emit_gate_error("index_coordination_unavailable", str(exc))
    raise AssertionError("unreachable: every gate failure above exits")  # pragma: no cover


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------


_GIT_TIMEOUT_S = 10  # max seconds for any git subprocess (H78: hung process guard)

# The single file-set contract shared by the index writer and BOTH staleness readers.
# scan-index records ``file_shas`` for exactly these patterns, so any reader that
# narrows the set reports "fresh" for a change it simply never looked at — the v2
# timestamp fallback used to watch ``*.py`` alone, so editing a ``.pyi`` or a doc file
# left a file_shas-less index claiming freshness. Spelled ONCE here and consumed by
# :func:`_resolve_current_file_shas` and :func:`check_staleness` so the two paths
# cannot drift apart again.
_INDEXED_PATHSPEC = INDEXED_PATHSPEC


def _git_cwd_kwargs() -> dict[str, str]:
    """Return the ``cwd`` kwarg that pins a git subprocess to the repository root.

    Every path this module compares against the index — ``file_shas`` keys, module
    paths, untracked-file paths — is recorded by scan-index relative to the git root.
    A git subprocess launched without ``cwd`` inherits the *process* CWD instead, so
    the same query run from a subdirectory got subdirectory-relative paths back: every
    stored path then read as "deleted" and every listed path as "added", reporting the
    index permanently stale, self-healing on every call, and answering
    ``query_complete: false`` forever. Anchoring here is what makes a query return the
    same answer from anywhere in the tree.

    Returns:
        ``{"cwd": <git root>}`` inside a repository, else ``{}`` — with no repository
        there is nothing to anchor to and the caller's CWD is the only root available.
    """
    git_root = _get_git_root_cached()
    return {"cwd": str(git_root)} if git_root is not None else {}


# Self-heal bounds: when the index is stale at query time we run
# ``scan-index --incremental`` inline so the answer reflects the current tree.
# Bounded so the heal never dominates the query path — a large change set or a
# slow scan falls back to the stale-honest result instead.
_HEAL_MAX_CHANGED_FILES = 50  # skip heal when more than this many .py files changed
_HEAL_TIMEOUT_S = 10  # hard wall-clock cap on the incremental scan subprocess


def _autobuild_disabled() -> bool:
    """Return whether the caller requires queries to use the existing index exactly as-is.

    ``SCAN_NO_AUTOBUILD=1`` is used by isolated benchmark and CI environments so index refresh work cannot leak into
    measured query cost. Only the documented value ``"1"`` opts out; an unset or malformed value preserves the
    interactive self-heal default.
    """
    return os.environ.get("SCAN_NO_AUTOBUILD") == "1"


class _FileShas(NamedTuple):
    """Tracked blob SHAs plus how confidently they were obtained.

    ``status`` is what separates "git says nothing changed" from "git never answered". Collapsing the two — the previous
    behaviour, an empty dict for both — let a git failure be read as proof of a fresh index.
    """

    shas: dict[str, str]
    status: str


#: git answered; ``shas`` is authoritative.
_SHAS_OK = "ok"
#: Not a git repository. Staleness is not knowable here and never was — no anomaly,
#: so this path stays silent (ZIP exports and non-git trees query without noise).
_SHAS_NO_REPO = "no_repo"
#: Inside a repository but git failed. Staleness is UNDETERMINED, not "fresh".
_SHAS_GIT_ERROR = "git_error"

_file_shas_cache: _FileShas | None = None


def _parse_ls_files_stage(output: str) -> dict[str, str]:
    """Return ``{path: blob_sha}`` parsed from ``git ls-files -s`` output.

    Drops user-excluded paths so the result matches the ``file_shas`` written by
    scan-index. Uses ``_match_exclusion`` only (NOT SKIP_DIRS): scan-index's git-blob
    ``file_shas`` path (``_git_file_hashes``) filters solely by user exclusions and
    keeps SKIP_DIR files that git tracks. Applying SKIP_DIRS here would drop those,
    making them show as "deleted" and re-introducing a false stale. Matching the
    writer exactly is the point.

    Args:
        output: raw stdout of ``git ls-files -s``, one ``<mode> <sha> <stage>\\t<path>``
            record per line.
    """
    exclusions = _get_exclusions_cached()
    shas: dict[str, str] = {}
    for line in output.strip().splitlines():
        meta, tab, path = line.partition("\t")
        fields = meta.split()
        # A record with no tab or fewer than two metadata fields is not a stage line;
        # skip it rather than raise — a malformed line must not abort the whole query.
        if not tab or not path or len(fields) < 2:
            continue
        if _match_exclusion(path, exclusions) is not None:
            continue
        shas[path] = fields[1]
    return shas


def _warn_staleness_undetermined(exc: BaseException) -> None:
    """Report that git failed *inside* a repository, so staleness could not be decided.

    Deliberately distinct from the no-repository case: here git was expected to answer
    and did not, so treating the empty result as "no files changed" would assert a
    fresh index as fact on no evidence. The query still answers — it simply stops
    claiming the answer is current.

    Args:
        exc: the failure raised by the git subprocess, named in the diagnostic so the
            cause (missing binary, timeout, non-zero exit) is visible to the caller.
    """
    _print(
        f"⚠ codemap: git could not be queried ({type(exc).__name__}) — index staleness is UNDETERMINED. "
        "This answer may reflect an out-of-date scan; re-run /codemap-py:scan-codebase to be sure.",
        file=sys.stderr,
    )


def _resolve_current_file_shas() -> _FileShas:
    """Read tracked source blob SHAs from git once, classifying any failure.

    Includes ``.py``, ``.pyi``, ``.rst``, and ``docs/**/*.md`` via :data:`_INDEXED_PATHSPEC` because each can affect the
    index.
    """
    git_root = _get_git_root_cached()
    if git_root is None:
        return _FileShas({}, _SHAS_NO_REPO)
    try:
        output = subprocess.check_output(
            ["git", "ls-files", "-s", "--", *_INDEXED_PATHSPEC],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
            cwd=str(git_root),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Narrow by design: OSError covers a missing/unexecutable git binary,
        # SubprocessError covers non-zero exit and timeout. Anything else is a real
        # defect in this module and must surface rather than masquerade as "fresh".
        _warn_staleness_undetermined(exc)
        return _FileShas({}, _SHAS_GIT_ERROR)
    return _FileShas(_parse_ls_files_stage(output), _SHAS_OK)


def _current_file_shas() -> _FileShas:
    """Return the memoized tracked-blob SHAs (and their status) for this invocation.

    Memoized because two independent consumers ask the same question on every query — :func:`_changed_py_files` for the
    self-heal decision and :func:`_coverage` for the honesty block. Without this memo, each query spawned two identical
    subprocesses running ``git ls-files``. The working tree cannot change under a single query, so one call is both
    cheaper and guaranteed self-consistent.
    """
    global _file_shas_cache
    if _file_shas_cache is None:
        _file_shas_cache = _resolve_current_file_shas()
    return _file_shas_cache


def _get_current_file_shas() -> dict[str, str]:
    """Return tracked source blob SHAs using the scanner's exact file-set contract.

    Thin accessor over :func:`_current_file_shas` for callers that only need the mapping; callers that must distinguish
    "nothing changed" from "git never answered" read ``.status`` instead.
    """
    return _current_file_shas().shas


def check_staleness(scanned_at: str) -> bool:
    """Return True if any indexed source file changed after scanned_at (timestamp fallback).

    Used only for a v2 index that predates ``file_shas``. Watches
    :data:`_INDEXED_PATHSPEC` — the writer's own file set — rather than a narrower
    hand-written list: an include of ``*.py`` plus ``:!docs/`` exclusions covered
    neither a changed ``.pyi``/``.rst``/doc file nor a ``.py`` under ``docs/``, all of
    which scan-index does index, so each edit left this check reporting "fresh".

    Args:
        scanned_at: ISO timestamp string from the index's ``scanned_at`` field.
    """
    # Validate scanned_at to only allow ISO 8601 timestamp chars (H79: defense-in-depth
    # against index-controlled value; subprocess list-form already prevents shell injection,
    # but reject obviously malformed values before passing to git)
    import re as _re

    if not _re.match(r"^[0-9T:+\-Z.]+$", scanned_at):
        return False
    try:
        result = subprocess.run(
            ["git", "log", f"--since={scanned_at}", "--name-only", "--pretty=", "--", *_INDEXED_PATHSPEC],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            **_git_cwd_kwargs(),
        )
        if result.returncode != 0:
            return False
        return bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _changed_py_files(index: dict) -> list[str] | None:
    """Return the list of tracked ``.py`` paths that differ from the index, or None.

    Uses the v3 ``file_shas`` blob diff (git-based). Returns None when the index
    predates ``file_shas`` or git is unavailable — callers fall back to the
    timestamp check. An empty list means "fresh"; a populated list means "stale".

    Args:
        index: parsed codemap index dict.
    """
    stored_shas = index.get("file_shas")
    if not stored_shas:
        return None
    current_shas = _get_current_file_shas()
    if not current_shas:
        return None
    changed = [p for p in current_shas if stored_shas.get(p) != current_shas[p]]
    added = [p for p in current_shas if p not in stored_shas]
    deleted = [p for p in stored_shas if p not in current_shas]
    return changed + added + deleted


def maybe_self_heal(index: dict, index_path: Path, scan_root: Path | None) -> dict:
    """Run a bounded incremental scan when the index is stale, then reload.

    a stale index at query time silently under-reports edges. When the
    change set is small enough (:data:`_HEAL_MAX_CHANGED_FILES`) we re-run
    ``scan-index --incremental`` inline and answer from the fresh graph. A large
    change set, a missing scan-index, a slow scan (:data:`_HEAL_TIMEOUT_S`), or a
    non-zero exit all fall back to the original stale index — the query still
    answers, honestly flagged stale.

    Args:
        index: the freshly loaded (possibly stale) index dict.
        index_path: path the index was loaded from, for reload after healing.
        scan_root: project root to hand scan-index via ``--root``; None omits it.

    Returns:
        The reloaded fresh index on a successful heal, else the original index.
    """
    changed = _changed_py_files(index)
    if not changed:  # None (no file_shas → can't bound safely) or [] (already fresh)
        return index
    if len(changed) > _HEAL_MAX_CHANGED_FILES:
        _print(
            f"⚠ codemap: {len(changed)} files changed (> {_HEAL_MAX_CHANGED_FILES} heal cap) — "
            "answering from the stale index. Run /codemap-py:scan-codebase --incremental to refresh.",
            file=sys.stderr,
        )
        return index

    # The caller's read lease is already released by the time we get here — the scan
    # below is a writer that takes its own exclusive lease, and a reader token still
    # held by this process would block it until its deadline expired, every time.
    if not _run_incremental_scan(_BIN / "scan-index", scan_root, len(changed)):
        return index
    try:
        healed = _load_index_leased(index_path)
    except (SystemExit, rwgate.IndexBusy, rwgate.CoordinationUnavailable):
        # A corrupt or momentarily unavailable reload must not abort a query that
        # already has a usable (if stale) answer in hand — heal is best-effort.
        return index
    _print(f"codemap: self-healed index ({len(changed)} file(s) re-scanned).", file=sys.stderr)
    return healed


def _run_incremental_scan(scan_index_bin: Path, scan_root: Path | None, changed_count: int) -> bool:
    """Run ``scan-index --incremental`` bounded by :data:`_HEAL_TIMEOUT_S`; return whether it succeeded.

    Args:
        scan_index_bin: path to the ``scan-index`` executable.
        scan_root: project root to hand via ``--root``; None omits it.
        changed_count: Number of source files known stale before this refresh.
    """
    if not scan_index_bin.exists():
        return False
    cmd = [sys.executable, str(scan_index_bin), "--incremental"]
    if scan_root is not None:
        cmd += ["--root", str(scan_root)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_HEAL_TIMEOUT_S,
            cwd=str(scan_root) if scan_root is not None else None,
            env={
                **os.environ,
                "CODEMAP_REFRESH_TRIGGER": "query_self_heal",
                "CODEMAP_REFRESH_CHANGED_COUNT": str(changed_count),
                "CODEMAP_REFRESH_STALE_BEFORE": "true",
            },
        )
    except (subprocess.TimeoutExpired, OSError):
        _print("⚠ codemap: incremental self-heal timed out — answering from the stale index.", file=sys.stderr)
        return False
    return result.returncode == 0


def warn_if_stale(index: dict) -> None:
    """Print a staleness warning to stderr if indexed files differ from the current working tree.

    Args:
        index: parsed codemap index dict containing ``file_shas`` or ``scanned_at``.
    """
    stored_shas = index.get("file_shas")
    if stored_shas:
        # v3 index: precise SHA-based comparison
        current_shas = _get_current_file_shas()
        if current_shas:
            changed = [p for p in current_shas if stored_shas.get(p) != current_shas[p]]
            added = [p for p in current_shas if p not in stored_shas]
            deleted = [p for p in stored_shas if p not in current_shas]
            stale_files = changed + added + deleted
            if stale_files:
                n = len(stale_files)
                _print(
                    f"⚠ codemap index stale — {n} file(s) changed since last scan."
                    " Run /codemap-py:scan-codebase --incremental to update.",
                    file=sys.stderr,
                )
            return
    # v2 index fallback: timestamp-based check
    scanned_at = index.get("scanned_at", "")
    if scanned_at and check_staleness(scanned_at):
        _print(
            "⚠ codemap index may be stale — Python files changed since last scan. Re-run /codemap-py:scan-codebase.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_module_map(index: dict) -> dict[str, dict]:
    """Return a name-keyed dict of all module entries in the index.

    Args:
        index: parsed codemap index dict.
    """
    return {m["name"]: m for m in index.get("modules", [])}


def build_symbol_map(index: dict, exclude_tests: bool = False) -> dict[str, tuple[dict, dict]]:
    """Flat lookup: ``full_qname -> (module_entry, symbol_dict)``.

    ``full_qname`` is ``module_name::symbol_qualified_name``.
    Degraded modules are skipped.

    Args:
        index: parsed codemap index dict.
    """
    result: dict[str, tuple[dict, dict]] = {}
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        if exclude_tests and m.get("is_test"):
            continue
        for sym in m.get("symbols", []):
            qname = f"{m['name']}::{sym['qualified_name']}"
            result[qname] = (m, sym)
    return result


def _resolve_symbol_alias(index: dict, qname: str) -> str | None:
    """Resolve a persisted static symbol alias without trusting malformed chains.

    Scan-index writes only canonical alias targets, but query must still treat an edited or otherwise malformed index
    defensively. A cycle therefore returns ``None`` rather than looping or inventing a target.
    """
    aliases = index.get("symbol_aliases", {})
    if not isinstance(aliases, dict):
        return None
    current = qname
    seen: set[str] = set()
    while current in aliases:
        if current in seen:
            return None
        seen.add(current)
        target = aliases[current]
        if not isinstance(target, str) or "::" not in target:
            return None
        current = target
    return current


def _symbol_alias_limitations(index: dict) -> list[dict[str, str]]:
    """Return validated persisted evidence for every rejected alias path."""
    records = index.get("symbol_alias_limitations", [])
    if not isinstance(records, list):
        return []
    matches: set[tuple[str, str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        alias_qname = record.get("alias_qname")
        target_qname = record.get("target_qname")
        reason = record.get("reason")
        if not all(isinstance(value, str) for value in (alias_qname, target_qname, reason)):
            continue
        resolved_target = _resolve_symbol_alias(index, target_qname)
        if resolved_target is not None:
            matches.add((alias_qname, resolved_target, reason))
    return [
        {"alias_qname": alias_qname, "target_qname": target_qname, "reason": reason}
        for alias_qname, target_qname, reason in sorted(matches)
    ]


def _alias_limitations_for_target(index: dict, query_target: str | None) -> list[dict[str, str]]:
    """Return persisted rejected-alias records that can hide callers of one target."""
    if not query_target:
        return []
    resolved_target = _resolve_symbol_alias(index, query_target)
    if resolved_target is None:
        return []
    return [record for record in _symbol_alias_limitations(index) if record["target_qname"] == resolved_target]


def build_reverse_call_graph(index: dict, exclude_tests: bool = False) -> dict[str, list[str]]:
    """Reverse call map: ``callee_full_qname -> [caller_full_qname, ...]``.

    Only edges with ``resolution`` of ``"import"`` or ``"local"`` are
    included (unresolved / external calls are excluded).

    Args:
        index: parsed codemap index dict with call graph data.
    """
    rev: dict[str, list[str]] = {}
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        if exclude_tests and m.get("is_test"):
            continue
        for sym in m.get("symbols", []):
            caller_qname = f"{m['name']}::{sym['qualified_name']}"
            for edge in sym.get("calls", []):
                if edge.get("resolution") in VALID_CALL_RESOLUTIONS:
                    target = _resolve_symbol_alias(index, edge["target"])
                    if target is None:
                        continue
                    rev.setdefault(target, []).append(caller_qname)
    return rev


# ---------------------------------------------------------------------------
# Module-level caches for expensive computed structures
# ---------------------------------------------------------------------------

_coverage_cache: dict | None = None
_symbol_map_cache: dict | None = None
_rev_graph_cache: dict | None = None

# set once in main() before any command runs. True when the index's stored
# scan_root does not match where this query is being resolved against (a mismatched
# ``--root``, or a CWD outside the scanned tree). A root-mismatched index answers about a
# DIFFERENT project, so its graph can never be a complete answer here — this flag both
# surfaces in every coverage block and forces query_complete=false in _query_complete.
_root_mismatch: bool = False


def _get_symbol_map(index: dict, exclude_tests: bool = False) -> dict:
    global _symbol_map_cache
    if _symbol_map_cache is None:
        _symbol_map_cache = build_symbol_map(index, exclude_tests)
    return _symbol_map_cache


def _get_rev_graph(index: dict, exclude_tests: bool = False) -> dict:
    global _rev_graph_cache
    if _rev_graph_cache is None:
        _rev_graph_cache = build_reverse_call_graph(index, exclude_tests)
    return _rev_graph_cache


_rev_import_graph_cache: dict | None = None


def _build_rev_import_graph_raw(index: dict) -> dict[str, list[str]]:
    """Reverse import map: module -> [modules that directly import it]."""
    rev: dict[str, list[str]] = {}
    for m in index.get("modules", []):
        for dep in m.get("direct_imports", []):
            rev.setdefault(dep, []).append(m["name"])
    return rev


def _get_rev_import_graph(index: dict) -> dict[str, list[str]]:
    global _rev_import_graph_cache
    if _rev_import_graph_cache is None:
        _rev_import_graph_cache = _build_rev_import_graph_raw(index)
    return _rev_import_graph_cache


# Exit-code contract: every error exit prints a parseable JSON object
# to stdout — never a bare non-zero exit with empty stdout. Codes let a caller branch
# on failure class without string-matching the message.
_EXIT_GENERIC = 1  # generic failure (missing symbol, invalid index, feature gate)
_EXIT_BAD_INPUT = 2  # caller-supplied argument is malformed or rejected (bad regex, ReDoS, guard)
_EXIT_NOT_INDEXED = 3  # queried module is absent from the index (distinct from "no results")


def _die_json(payload: dict, exit_code: int = _EXIT_GENERIC) -> None:
    """Print a JSON error object to stdout and exit with *exit_code*.

    Single choke point for every error exit so all failures share one shape
    (parseable JSON on stdout, never empty) and a stable exit-code contract.
    Routing through :func:`_print` keeps the cli.jsonl telemetry record.

    Args:
        payload: JSON-serialisable error object; ``error`` key is conventional.
        exit_code: process exit status (see the ``_EXIT_*`` constants).

    Examples:
        >>> import subprocess, sys
        >>> # _die_json({"error": "boom"}, 2) prints '{"error": "boom"}' then exits 2.
    """
    _print(json.dumps(payload))
    sys.exit(exit_code)


def _exit_error(message: str) -> None:
    """Print a ``{"error": message}`` object to stdout and exit with code 1.

    Backward-compatible wrapper over :func:`_die_json` preserving the historic
    shape and exit code for the many callers that only need a plain message.

    Args:
        message: human-readable error description.
    """
    _die_json({"error": message}, _EXIT_GENERIC)


def _die_module_not_indexed(index: dict, module: str) -> None:
    """Exit 3 with a structured "module not indexed" error plus close suggestions.

    Replaces the historic ``Module 'X' not in index.`` message: a
    caller can now distinguish "this module is absent from the index" from "this
    module exists but has no results", and gets up to three closest indexed module
    names (difflib) to recover from a typo without a second round-trip.

    Args:
        index: parsed codemap index dict (source of the candidate module names).
        module: the dotted module name that was not found in the index.
    """
    import difflib

    known = [m["name"] for m in index.get("modules", []) if "name" in m]
    suggestions = difflib.get_close_matches(module, known, n=3, cutoff=0.6)
    _die_json(
        {"error": "module not indexed", "module": module, "suggestions": suggestions},
        _EXIT_NOT_INDEXED,
    )


def _require_call_graph(index: dict) -> None:
    """Exit with a clear error if the index lacks call graph data (v2 index).

    Args:
        index: parsed codemap index dict to check for v3 call graph support.
    """
    if not _has_call_graph(index):
        _exit_error("Index is v2 — call graph not available. Re-run /codemap-py:scan-codebase to upgrade.")


def _require_feature(index: dict, min_ver: int, feature: str) -> None:
    """Exit with a clear error if the index predates *feature*.

    Args:
        index: parsed codemap index dict.
        min_ver: minimum scan_version the feature requires.
        feature: human-readable feature name shown in the error message.
    """
    raw = index.get("scan_version", 0)
    try:
        version = int(raw)
    except (TypeError, ValueError):
        version = 0
    if version < min_ver:
        _exit_error(f"'{feature}' requires index version {min_ver} (current: {version}). Re-run scan-index to rebuild.")


def _current_git_sha() -> str | None:
    """Return current HEAD SHA, or None if git is unavailable."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _untracked_py_files() -> list[str]:
    """Return new, git-untracked ``.py`` files, or [] if git is unavailable.

    Documented blind spot: ``file_shas`` is git-blob based, so a brand-new
    ``.py`` file that has never been ``git add``-ed is invisible to the
    staleness diff. Surface it in the coverage block so a whole-graph/global-in
    query never claims completeness while such a file exists.

    F4: untracked files inside an excluded dir (e.g. a vendored tree or ``.claude/``)
    are dropped — scan-index would never have indexed them, so they must not poison
    ``query_complete`` either.

    Paths come back relative to the git root (see :func:`_git_cwd_kwargs`) so they can
    be compared directly against the index's module paths. Left unanchored, a query
    from a subdirectory got subdirectory-relative paths that matched no indexed path,
    so every untracked file registered as an unindexed blind spot and vetoed
    ``query_complete`` for the whole session.

    Scope note: the pathspec stays ``*.py`` rather than :data:`_INDEXED_PATHSPEC`
    because this list feeds the *blind-spot* veto, which is about graph nodes. A
    stray untracked ``.rst`` or doc file cannot hide an import edge, and widening the
    set here would veto completeness for documentation churn.
    """
    try:
        output = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
            **_git_cwd_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    exclusions = _get_exclusions_cached()
    return [line for line in output.strip().splitlines() if line and not is_excluded(line, exclusions)]


def _indexed_untracked_modified(paths: list[str], scanned_at: str) -> bool:
    """Return True when any indexed-but-untracked file was modified after the scan.

    Untracked files have no git blob, so the SHA-based staleness diff cannot see
    their edits; the file mtime against the index ``scanned_at`` timestamp is the
    only signal. Unparsable timestamps or unstatable paths are skipped (fail-open
    to "not modified" — the file is still surfaced via the index itself).

    Args:
        paths: indexed-but-git-untracked ``.py`` paths, relative to the git root
            (:func:`_untracked_py_files` anchors them there), so they are resolved
            against that root rather than the process CWD — statting them CWD-relative
            silently found nothing whenever a query ran from a subdirectory, and a
            missing path fails open to "not modified".
        scanned_at: the index's ISO-8601 ``scanned_at`` timestamp.
    """
    if not paths or not scanned_at:
        return False
    root = _get_git_root_cached() or Path.cwd()
    try:
        # scanned_at is UTC (scan-index: datetime.now(timezone.utc).isoformat()) —
        # timegm keeps the comparison in UTC; mktime would shift by the local offset.
        scanned_epoch = calendar.timegm(time.strptime(scanned_at[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return False
    for p in paths:
        try:
            # +1s slack: scanned_at is floored to whole seconds ([:19]), so a file
            # written in the same second as the scan would otherwise flag falsely.
            if (root / p).stat().st_mtime > scanned_epoch + 1:
                return True
        except OSError:
            continue
    return False


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
    untracked_all = _untracked_py_files()
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
        "root_mismatch": _root_mismatch,
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
_LOCAL_DIRECTION_CMDS = frozenset({"deps", "symbols"})
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
) -> str:
    """Build a human note that never contradicts the emitted ``query_complete``.

    F1: the note must track the direction-scoped flag, not the direction-agnostic
    stale/degraded facts alone — otherwise an untracked/collision veto could ship
    "This result is complete" next to ``query_complete: false``.

    Args:
        base: the shared coverage dict from :func:`_coverage`.
        complete: the decided ``query_complete`` value for this command.
        reason: the veto slug from :func:`_query_complete` (``"ok"`` when complete).
        alias_limitations_total: number of relevant rejected alias paths.
        alias_limitations_truncated: whether compact output emits only the bounded sample.
    """
    total = base["total_modules"]
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


# ---------------------------------------------------------------------------
# coverage diet: session-scoped once-per-session full block, compact after
# ---------------------------------------------------------------------------
#
# The full coverage block repeats ~15 identical keys on EVERY query result. Within
# one Claude Code session that's pure token waste after the first read: the agent
# has already seen total_modules/degraded_files/note once and only needs the
# per-query honesty signals thereafter. So the FIRST query per session emits the
# full block; every later query in the same session emits a compact block carrying
# only the fields that can change per query (query_complete, stale, root_mismatch)
# plus the incompleteness reason (degraded count + note) so the diet never hides
# WHY a result is incomplete.
#
# Session identity comes from the hook-written marker (cross-agent contract): the
# CLI cannot see session_id itself (no session env var in the Bash tool env; pid
# changes per invocation so pid-keying can never dedup across queries). Marker
# missing / unparsable / older than the TTL → treat as absent → always full block
# (fail-verbose: never silently compact when we can't prove same-session).

_SESSION_MARKER_TTL_MS = 30 * 60 * 1000  # 30 min — matches the hook writer's guard
_verbose_coverage: bool = False  # set from ``--verbose-coverage`` in main(); forces full block
_force_compact_coverage: bool = False  # set from ``--compact`` in main(); opt-in coverage diet
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
    git_root = _get_git_root_cached()
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
    if _force_compact_coverage:
        return True
    if _verbose_coverage:
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
        **extra: per-command fields (method, not_covered, hint, scope, etc.).
    """
    base = _coverage(index)
    resolved_command = command or _CMD
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
        if _LOADED_INDEX_PATH:
            compact["index_path"] = _LOADED_INDEX_PATH
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
    if _LOADED_INDEX_PATH:
        full["index_path"] = _LOADED_INDEX_PATH
    if relevant:
        full["degraded_relevant"] = relevant
    return {**full, **extra, **({"symbol_alias_limitations": alias_limitations} if alias_limitations else {})}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_deps(
    index: dict,
    module: str,
    stdlib_only: bool = False,
    third_party_only: bool = False,
    internal_only: bool = False,
) -> None:
    """Print direct imports of a module as JSON, optionally filtered by import group.

    When any of *stdlib_only*, *third_party_only*, *internal_only* is True the
    returned ``direct_imports`` list is restricted to the selected group(s) using
    the per-module ``import_groups`` field (requires v4.3+ index). When no
    filter flag is set, all imports are returned and no feature check fires
    (backward-compatible with v4.0–v4.2 indexes).

    Args:
        index: parsed codemap index dict.
        module: dotted module name to look up.
        stdlib_only: if True, restrict result to imports classified as stdlib.
        third_party_only: if True, restrict result to imports classified as third-party.
        internal_only: if True, restrict result to imports classified as internal.
    """
    modules = build_module_map(index)
    entry = modules.get(module)
    if entry is None:
        _die_module_not_indexed(index, module)
    direct = entry.get("direct_imports", [])
    if stdlib_only or third_party_only or internal_only:
        _require_feature(index, IMPORT_GROUPS_MIN_VER, "import_groups")
        groups = entry.get("import_groups", {"stdlib": [], "third_party": [], "internal": []})
        selected: set[str] = set()
        if stdlib_only:
            selected.update(groups.get("stdlib", []))
        if third_party_only:
            selected.update(groups.get("third_party", []))
        if internal_only:
            selected.update(groups.get("internal", []))
        # Preserve original order from direct_imports while restricting to the selected set.
        direct = [imp for imp in direct if imp in selected]
    _print(
        json.dumps(
            {
                "module": module,
                "direct_imports": direct,
                "index": _cmd_coverage(
                    index,
                    method="import-graph",
                    module_status=entry.get("status"),
                    module_name=module,
                    not_covered=_IMPORT_GRAPH_NOT_COVERED,
                ),
            }
        )
    )


def cmd_import_types(index: dict, module: str) -> None:
    """Print all three import groups (stdlib, third_party, internal) for a module.

    Requires v4.3+ index (``import_groups`` field). Each group preserves the
    original import strings as recorded by scan-index — no normalisation.

    Args:
        index: parsed codemap index dict (must be v4.3+ with import_groups).
        module: dotted module name to look up.
    """
    _require_feature(index, IMPORT_GROUPS_MIN_VER, "import_groups")
    modules = build_module_map(index)
    entry = modules.get(module)
    if entry is None:
        _die_module_not_indexed(index, module)
    groups = entry.get("import_groups", {"stdlib": [], "third_party": [], "internal": []})
    _print(
        json.dumps(
            {
                "module": module,
                "stdlib": groups.get("stdlib", []),
                "third_party": groups.get("third_party", []),
                "internal": groups.get("internal", []),
                "index": _cmd_coverage(index, method="import-graph", not_covered=_IMPORT_GRAPH_NOT_COVERED),
            }
        )
    )


def cmd_rdeps(
    index: dict,
    module: str,
    exclude_tests: bool = False,
    entity: EntityType | None = None,
    limit: int = 0,
) -> None:
    """Print all modules that import a given module as JSON.

    Includes static importers (``imported_by``), dynamic importers
    (``dynamic_imported_by`` — from ``importlib.import_module`` / ``__import__`` string literals),
    and config-file references (``config_refs`` — from ``pyproject.toml``, ``setup.cfg``, etc.).

    Args:
        index: parsed codemap index dict.
        module: dotted module name whose reverse dependencies are queried.
        exclude_tests: if True, exclude test modules from static results.
        entity: if set, restrict importers to this :class:`EntityType`.
        limit: maximum static importers to return; 0 keeps the exhaustive default.
    """
    modules_list = index.get("modules", [])
    if exclude_tests:
        modules_list = [m for m in modules_list if not m.get("is_test")]
    if entity:
        modules_list = [m for m in modules_list if _entity_type(m) == entity]
    result = sorted(m["name"] for m in modules_list if module in m.get("direct_imports", []))
    total_available = len(result)
    truncated = limit > 0 and total_available > limit
    if truncated:
        result = result[:limit]
    module_map = build_module_map(index)
    entry = module_map.get(module, {})
    dynamic = entry.get("dynamic_imported_by", [])
    config = entry.get("config_refs", [])
    # a module absent from the index AND imported by nothing is "not indexed",
    # not "no reverse deps" — error with suggestions instead of a misleading empty
    # ``imported_by: []``. An indexed leaf with zero importers legitimately keeps the
    # empty list (it IS in module_map); an external dep someone imports is kept too
    # (it shows up in result/dynamic/config even though it has no own module entry).
    if module not in module_map and not (result or dynamic or config):
        _die_module_not_indexed(index, module)
    coverage = _cmd_coverage(
        index,
        query_target=module,
        method="import-graph",
        not_covered=_IMPORT_GRAPH_NOT_COVERED,
        **({"confidence": "partial"} if truncated else {}),
    )
    if truncated:
        coverage["truncated"] = True
        coverage["total_available"] = total_available
    _print(
        json.dumps(
            {
                "module": module,
                "imported_by": result,
                "dynamic_imported_by": dynamic,
                "config_refs": config,
                "index": coverage,
            }
        )
    )


def _production_rdep_counts(index: dict) -> dict[str, int]:
    """Return incoming import counts after removing every test-module edge.

    ``rdep_count`` is stored during index construction and intentionally includes test importers. Using ``central`` with
    ``--exclude-tests`` needs a separate count so it describes the production import graph rather than merely hiding
    test candidates. Module aliases use the same canonicalization as graph metrics.
    """
    aliases = index.get("module_aliases", {})
    counts: dict[str, int] = {}
    for importer in index.get("modules", []):
        if importer.get("is_test"):
            continue
        for imported in importer.get("direct_imports", []):
            target = aliases.get(imported, imported)
            counts[target] = counts.get(target, 0) + 1
    return counts


def cmd_central(index: dict, top: int, exclude_tests: bool = False, entity: EntityType | None = None) -> None:
    """Print the top N most-imported modules ranked by reverse-dependency count.

    Args:
        index: parsed codemap index dict.
        top: number of top-ranked modules to return.
        exclude_tests: if True, exclude test modules and their importer edges.
        entity: if set, restrict to this :class:`EntityType`.
    """
    candidates = [m for m in index.get("modules", []) if m.get("status") != "degraded"]
    if exclude_tests:
        candidates = [m for m in candidates if not m.get("is_test")]
    if entity:
        candidates = [m for m in candidates if _entity_type(m) == entity]
    if exclude_tests:
        rdep_counts = _production_rdep_counts(index)
        ranked = sorted(candidates, key=lambda m: (-rdep_counts.get(m["name"], 0), m["name"]))[:top]
    else:
        rdep_counts = {}
        ranked = sorted(candidates, key=lambda m: m.get("rdep_count", 0), reverse=True)[:top]
    _print(
        json.dumps(
            {
                "central": [
                    {
                        "name": m["name"],
                        "rdep_count": rdep_counts.get(m["name"], m.get("rdep_count", 0)),
                        "path": m.get("path", ""),
                    }
                    for m in ranked
                ],
                "index": _cmd_coverage(
                    index,
                    method="import-graph",
                    scope="import-centrality",
                    not_covered=_IMPORT_GRAPH_NOT_COVERED,
                ),
            }
        )
    )


def cmd_coupled(index: dict, top: int, exclude_tests: bool = False, entity: EntityType | None = None) -> None:
    """Print the top N most-coupled modules ranked by internal import count.

    Args:
        index: parsed codemap index dict.
        top: number of top-ranked modules to return.
        exclude_tests: if True, exclude test modules from results.
        entity: if set, restrict to this :class:`EntityType`.
    """
    candidates = [m for m in index.get("modules", []) if m.get("status") != "degraded"]
    if exclude_tests:
        candidates = [m for m in candidates if not m.get("is_test")]
    if entity:
        candidates = [m for m in candidates if _entity_type(m) == entity]
    all_module_names = {m["name"] for m in index.get("modules", []) if m.get("status") == "ok"}
    # compute internal_count locally — never mutate the loaded index dict
    internal_counts = {
        m["name"]: sum(1 for i in m.get("direct_imports", []) if i in all_module_names) for m in candidates
    }
    ranked = sorted(candidates, key=lambda m: internal_counts.get(m["name"], 0), reverse=True)[:top]
    _print(
        json.dumps(
            {
                "coupled": [
                    {
                        "name": m["name"],
                        "dep_count": m.get("dep_count", 0),
                        "internal_dep_count": internal_counts.get(m["name"], 0),
                        "path": m.get("path", ""),
                    }
                    for m in ranked
                ],
                "index": _cmd_coverage(
                    index,
                    method="import-graph",
                    scope="coupling-score",
                    not_covered=_IMPORT_GRAPH_NOT_COVERED,
                ),
            }
        )
    )


def cmd_path(index: dict, frm: str, to: str) -> None:
    """Print the shortest import path between two modules via BFS.

    Both endpoints exist in the index (unknown modules exit ``3`` via
    :func:`_die_module_not_indexed`). When no import path connects them, this
    exits ``0`` with ``"path": null`` and ``"reason": "no-import-path"`` — a
    legitimate empty result, distinct from the ``"error"`` contract used for
    failures.

    Args:
        index: parsed codemap index dict.
        frm: source module name.
        to: target module name.
    """
    modules = build_module_map(index)
    if frm not in modules:
        _die_module_not_indexed(index, frm)
    if to not in modules:
        _die_module_not_indexed(index, to)

    queue: deque[list[str]] = deque([[frm]])
    visited: set[str] = {frm}
    while queue:
        path = queue.popleft()
        node = path[-1]
        if node == to:
            _print(
                json.dumps(
                    {
                        "from": frm,
                        "to": to,
                        "path": path,
                        "index": _cmd_coverage(
                            index,
                            method="import-graph",
                            not_covered=_IMPORT_GRAPH_NOT_COVERED,
                        ),
                    }
                )
            )
            return
        for neighbour in modules.get(node, {}).get("direct_imports", []):
            if neighbour not in visited and neighbour in modules:
                visited.add(neighbour)
                queue.append(path + [neighbour])

    _print(
        json.dumps(
            {
                "from": frm,
                "to": to,
                "path": None,
                "reason": "no-import-path",
                "index": _cmd_coverage(index, method="import-graph", not_covered=_IMPORT_GRAPH_NOT_COVERED),
            }
        )
    )


def cmd_list(index: dict, limit: int = 100) -> None:
    """Print indexed modules with their paths and status, capped at *limit*.

    diet: a large repo's full module list is the single biggest scan-query
    result. The default cap keeps the common ``list`` call small while ``total``
    and ``shown`` disclose the truncation so a caller knows to raise ``--limit``
    (or pass ``0`` for the full list) when it genuinely needs every module.

    Args:
        index: parsed codemap index dict.
        limit: max modules to emit; 0 returns all (default 100).
    """
    all_modules = [
        {"name": m["name"], "path": m.get("path", ""), "status": m.get("status", "ok")}
        for m in index.get("modules", [])
    ]
    total = len(all_modules)
    modules = all_modules if limit <= 0 else all_modules[:limit]
    _print(json.dumps({"modules": modules, "total": total, "shown": len(modules)}))


def _entity_type(m: dict) -> str:
    """Return entity_type for a module, with fallback for pre-v5.5 indexes.

    Stays a plain ``str``: the value is read back from an on-disk index that may have
    been written by an older or foreign writer, so it is not guaranteed to be an
    :class:`EntityType` member. Compare it against members with ``==``.

    Args:
        m: module entry dict from the index.
    """
    et = m.get("entity_type")
    if et:
        return et
    return EntityType.TEST.value if m.get("is_test") else EntityType.PKG.value


def _as_entity(raw: str | None) -> EntityType | None:
    """Convert an ``--entity`` CLI value to its member (argparse already gated the choices)."""
    return EntityType(raw) if raw else None


def cmd_packages(index: dict) -> None:
    """Print top-level packages with module counts broken down by entity_type.

    For indexes predating v5.5 (no entity_type field), entity is derived from
    is_test (test vs pkg); docs and example are not distinguished.

    Args:
        index: parsed codemap index dict.
    """
    modules = [m for m in index.get("modules", []) if m.get("status") == "ok"]
    pkg_data: dict[str, dict] = {}
    for m in modules:
        pkg = m.get("package") or m["name"].split(".")[0]
        entity = _entity_type(m)
        if pkg not in pkg_data:
            pkg_data[pkg] = {"total": 0, "by_entity": {}}
        pkg_data[pkg]["total"] += 1
        by_entity = pkg_data[pkg]["by_entity"]
        by_entity[entity] = by_entity.get(entity, 0) + 1
    sorted_pkgs = sorted(pkg_data.items(), key=lambda kv: kv[1]["total"], reverse=True)
    _print(
        json.dumps(
            {
                "packages": [
                    {"name": pkg, "total": data["total"], "by_entity": data["by_entity"]} for pkg, data in sorted_pkgs
                ],
                "index": _cmd_coverage(index, method="entity-map"),
            }
        )
    )


def _resolve_project_root(explicit_root: Path | None, index: dict) -> Path:
    """Resolve project root for file path lookups using priority chain.

    Priority: explicit ``--root`` flag > scan_root stored in index > git root from CWD > CWD.
    """
    if explicit_root is not None:
        return explicit_root.resolve()
    stored = index.get("scan_root")
    if stored:
        return Path(stored)
    return _get_git_root_cached() or Path.cwd()


def _detect_root_mismatch(explicit_root: Path | None, index: dict) -> bool:
    """Return True when the query is being resolved against a different tree than the index.

    ``_resolve_project_root`` prefers the index's own ``scan_root`` when no
    ``--root`` is given, so it can never surface a mismatch on its own. This compares
    the index's stored ``scan_root`` against where the caller actually is — ``--root``
    if supplied, otherwise the git root of the CWD (fallback: CWD) — and reports when
    they diverge. An index with no ``scan_root`` (older builds) never mismatches.

    Args:
        explicit_root: the ``--root`` value if the caller passed one, else None.
        index: parsed codemap index dict (source of the stored ``scan_root``).
    """
    stored = index.get("scan_root")
    if not stored:
        return False
    stored_root = Path(stored).resolve()
    queried = (
        explicit_root.resolve() if explicit_root is not None else (_get_git_root_cached() or Path.cwd())
    ).resolve()
    return stored_root != queried


# coarse classification of a stale symbol coordinate. The fine-grained
# ``stale_reason`` is kept for diagnostics; ``stale_category`` gives agents the one
# bit that changes their next action — the symbol is GONE (``symbol_deleted``: its
# file or definition no longer exists → stop looking) versus MOVED (``coords_stale``:
# it still exists but the indexed line range is wrong → re-scan / Read the file).
_STALE_CATEGORY = {
    "no path": "symbol_deleted",
    "file deleted": "symbol_deleted",
    "line range past EOF": "coords_stale",
    "symbol name not in slice header": "coords_stale",
}


def _scan_symbols(index: dict, exclude_tests: bool, predicate: Callable[[Symbol], bool]) -> list[tuple[dict, Symbol]]:
    """Return every ``(module, symbol)`` pair across non-degraded modules where *predicate* holds.

    Args:
        index: parsed codemap index dict.
        exclude_tests: if True, skip test modules during the scan.
        predicate: called with each symbol dict; included when it returns True.
    """
    matches: list[tuple[dict, Symbol]] = []
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        if exclude_tests and m.get("is_test"):
            continue
        for sym in m.get("symbols", []):
            if predicate(sym):
                matches.append((m, sym))
    return matches


def _find_symbol_matches(index: dict, name: str, exclude_tests: bool) -> list[tuple[dict, Symbol]]:
    """Find symbols by exact name/qualified_name match, falling back to a substring search.

    Args:
        index: parsed codemap index dict.
        name: symbol name or qualified name to search for.
        exclude_tests: if True, skip test modules during search.
    """
    matches = _scan_symbols(index, exclude_tests, lambda sym: sym["name"] == name or sym["qualified_name"] == name)
    if matches:
        return matches
    # Fallback: case-insensitive substring on qualified_name
    name_lower = name.lower()
    return _scan_symbols(index, exclude_tests, lambda sym: name_lower in sym["qualified_name"].lower())


def _extract_import_block(lines: list[str], file_path: Path | None) -> str:
    """Return the module-level import statements from *lines*, or ``""`` on any parse failure.

    Args:
        lines: the file's source, split into lines (empty when the file is unreadable).
        file_path: the file's path, used only as the AST parse filename for error messages.
    """
    if not lines:
        return ""
    try:
        tree = ast.parse("\n".join(lines), filename=str(file_path or ""))
        collected: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                end = node.end_lineno or node.lineno
                collected.extend(lines[node.lineno - 1 : end])
        return "\n".join(collected)
    except (SyntaxError, ValueError):  # ValueError on null bytes
        return ""


def _symbol_source_and_staleness(
    sym: Symbol, lines: list[str], rel_path: str, file_path: Path | None
) -> tuple[str, bool, str | None]:
    """Slice *sym*'s source out of *lines* and detect whether that slice is stale.

    A stale slice is blanked before returning: never emit source that may
    point at a different function because the file shrank or moved since indexing. Instead,
    callers fall back to ``Read(path)`` and see ``stale_reason`` for diagnostics.

    Args:
        sym: the symbol dict (``start_line``, ``end_line``, ``name``).
        lines: the owning file's source lines (empty when the file is unreadable).
        rel_path: the module's indexed path (empty string when the index has none).
        file_path: resolved absolute path to the file, or None when *rel_path* is empty.
    """
    source = "\n".join(lines[sym["start_line"] - 1 : sym["end_line"]]) if lines else ""
    stale = False
    stale_reason: str | None = None
    if not rel_path:
        stale, stale_reason = True, "no path"
    elif file_path is None or not file_path.exists():
        stale, stale_reason = True, "file deleted"
    elif sym["end_line"] > len(lines):
        stale, stale_reason = True, "line range past EOF"
    elif source:
        # Identifier-boundary check: name must follow def/class keyword exactly.
        # scan-index records start_line at the def/class line (not decorator), so
        # first non-blank line is always the signature. Regex prevents foo matching foo_bar.
        first_nonblank = next((ln for ln in source.split("\n") if ln.strip()), "")
        name_pattern = re.compile(rf"\b(?:def|async\s+def|class)\s+{re.escape(sym['name'])}\s*[:(]")
        if not name_pattern.search(first_nonblank):
            stale, stale_reason = True, "symbol name not in slice header"
    if stale:
        source = ""
    return source, stale, stale_reason


def _symbol_group_results(
    rel_path: str, group: list[tuple[dict, dict]], git_root: Path, with_imports: bool
) -> list[dict]:
    """Build the result entries for every ``(module, symbol)`` pair sharing one file path.

    Reads *rel_path* at most once (empty ``lines`` when unreadable), optionally extracts
    its import block, then slices and staleness-checks each symbol in *group*.

    Args:
        rel_path: the module's indexed path (may be empty for a path-less entry).
        group: the ``(module_entry, symbol)`` pairs recorded under this path.
        git_root: project root used to resolve *rel_path* to an absolute path.
        with_imports: if True, attach the module-level import block to each result.
    """
    lines: list[str] = []
    file_path: Path | None = None
    if rel_path:
        file_path = git_root / rel_path
        if file_path.exists():
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()

    import_block: str | None = _extract_import_block(lines, file_path) if with_imports else None

    results = []
    for m, sym in group:
        source, stale, stale_reason = _symbol_source_and_staleness(sym, lines, rel_path, file_path)
        results.append(
            {
                "name": sym["name"],
                "qualified_name": sym["qualified_name"],
                "type": sym["type"],
                "module": m["name"],
                "path": m.get("path", ""),
                "start_line": sym["start_line"],
                "end_line": sym["end_line"],
                "source": source,
                "stale": stale,
                "stale_reason": stale_reason,
                "stale_category": _STALE_CATEGORY.get(stale_reason) if stale_reason else None,
                "imports": import_block,
            }
        )
    return results


def cmd_symbol(
    index: dict,
    name: str,
    limit: int = 20,
    exclude_tests: bool = False,
    with_imports: bool = False,
    project_root: Path | None = None,
) -> None:
    """Find a symbol by name (exact match, then qualified_name substring) and return its source.

    Args:
        index: parsed codemap index dict.
        name: symbol name or qualified name to search for.
        limit: max results to return (0 = unlimited).
        exclude_tests: if True, skip test modules during search.
        with_imports: if True, attach the module-level import block to each symbol result.
        project_root: resolved project root for file-path lookups; falls back to the
            cached git root, then CWD, when omitted.
    """
    matches = _find_symbol_matches(index, name, exclude_tests)
    if not matches:
        _exit_error(f"Symbol '{name}' not found. Try /codemap-py:query-code find-symbol <pattern> to search.")

    total_matches = len(matches)
    truncated = limit > 0 and total_matches > limit
    if truncated:
        matches = matches[:limit]

    # Group by file path to read each file at most once
    from collections import defaultdict

    by_path: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for m, sym in matches:
        by_path[m.get("path", "")].append((m, sym))

    git_root = project_root if project_root is not None else (_get_git_root_cached() or Path.cwd())
    results = [
        r for rel_path, group in by_path.items() for r in _symbol_group_results(rel_path, group, git_root, with_imports)
    ]
    any_stale = any(r["stale"] for r in results)
    confidence = "exact" if not truncated and not any_stale else "partial"
    coverage = _cmd_coverage(index, method="index-lookup", confidence=confidence)
    if truncated:
        coverage["truncated"] = True
        coverage["total_available"] = total_matches
    _print(
        json.dumps(
            {
                "symbols": results,
                "count": len(results),
                "index": coverage,
            }
        )
    )


def cmd_symbols(index: dict, module: str) -> None:
    """List all symbols in a module (no file I/O -- index only).

    Args:
        index: parsed codemap index dict.
        module: dotted module name whose symbols are listed.
    """
    modules = build_module_map(index)
    entry = modules.get(module)
    if entry is None:
        _die_module_not_indexed(index, module)
    syms: list[Symbol] = entry.get("symbols", [])
    _print(
        json.dumps(
            {
                "module": module,
                "path": entry.get("path", ""),
                "symbols": [
                    {
                        "name": s["name"],
                        "qualified_name": s["qualified_name"],
                        "type": s["type"],
                        "start_line": s["start_line"],
                        "end_line": s["end_line"],
                    }
                    for s in syms
                ],
                "count": len(syms),
                "index": _cmd_coverage(
                    index,
                    method="index-lookup",
                    module_status=entry.get("status"),
                    module_name=module,
                    confidence="exact",
                ),
            }
        )
    )


_DANGEROUS_PATTERN = re.compile(
    (
        "\n"
        r"    # adjacent quantifiers: a++, a**, a*+, a+{2,}"
        "\n"
        r"    (?:\+|\*|\{[0-9]+,?\})\s*(?:\+|\*|\{[0-9]+,?\})"
        "\n"
        r"    |"
        "\n"
        r"    # group with inner quantifier then outer quantifier: (a+)+, (a+)*, (.+){2,}"
        "\n"
        r"    \([^)]*(?:\+|\*|\?|\{[0-9])[^)]*\)\s*(?:\+|\*|\?|\{[0-9])"
        "\n    "
    ),
    re.VERBOSE,
)

# Alternation-based catastrophic backtracking that _DANGEROUS_PATTERN misses:
# any single-level alternation group followed by an outer quantifier — e.g.
# (a|aa)+, (a*|b*)+, (foo|foo)*. Overlapping or quantified branches under an outer
# +/*/{ are the classic exponential-backtracking shape. [^()] keeps this to a flat
# (non-nested) group so the match stays anchored to one alternation.
_ALT_REDOS_RE = re.compile(r"\([^()]*\|[^()]*\)[+*{]")


def _is_dangerous_regex(pattern: str) -> bool:
    """Return True if *pattern* exhibits a known catastrophic-backtracking shape.

    Combines the adjacent/nested-quantifier heuristic (:data:`_DANGEROUS_PATTERN`)
    with alternation-based ReDoS detection (:data:`_ALT_REDOS_RE`). Conservative
    by design — a false positive only rejects a query, never executes a hang.

    Examples:
        >>> _is_dangerous_regex("(a+)+")
        True
        >>> _is_dangerous_regex("(a|aa)+")
        True
        >>> _is_dangerous_regex("(a*|b*)+")
        True
        >>> _is_dangerous_regex("^Auth.*Handler$")
        False
    """
    return bool(_DANGEROUS_PATTERN.search(pattern) or _ALT_REDOS_RE.search(pattern))


def cmd_find_symbol(index: dict, pattern: str, limit: int = 20, exclude_tests: bool = False) -> None:
    """Regex search across all symbol qualified_names in the index.

    Args:
        index: parsed codemap index dict.
        pattern: Python regex pattern matched against each symbol's qualified name.
        limit: max results to return (0 = unlimited).
    """
    # ReDoS guard — nested/adjacent and alternation quantifiers (e.g. `(a+)+`, `(.*){2,}`,
    # `(a|aa)+`) can cause catastrophic backtracking. a bare stderr `return` left
    # stdout empty and exited 0, breaking JSON consumers — emit a parseable error object
    # and a non-zero exit so a rejection is unmistakable both to humans and to callers.
    if _is_dangerous_regex(pattern):
        _print(
            f"find-symbol: pattern '{pattern}' may cause ReDoS — use a simpler pattern",
            file=sys.stderr,
        )
        _die_json(
            {"error": "pattern rejected", "reason": "redos", "pattern": pattern},
            _EXIT_BAD_INPUT,
        )
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        _die_json({"error": "invalid regex", "pattern": pattern, "detail": str(exc)}, _EXIT_BAD_INPUT)

    results = []
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        if exclude_tests and m.get("is_test"):
            continue
        for sym in m.get("symbols", []):
            if rx.search(sym["qualified_name"]):
                results.append(
                    {
                        "name": sym["name"],
                        "qualified_name": sym["qualified_name"],
                        "type": sym["type"],
                        "module": m["name"],
                        "path": m.get("path", ""),
                        "start_line": sym["start_line"],
                        "end_line": sym["end_line"],
                    }
                )
    total_matches = len(results)
    truncated = limit > 0 and total_matches > limit
    if truncated:
        results = results[:limit]
    confidence = "exact" if not truncated else "partial"
    coverage = _cmd_coverage(index, method="index-lookup", confidence=confidence)
    if truncated:
        coverage["truncated"] = True
        coverage["total_available"] = total_matches
    _print(
        json.dumps(
            {
                "pattern": pattern,
                "matches": results,
                "count": len(results),
                "index": coverage,
            }
        )
    )


# ---------------------------------------------------------------------------
# Function-level commands (require v3 index with call graph)
# ---------------------------------------------------------------------------


def _reject_multiline_args(args: argparse.Namespace) -> None:
    """Exit with an actionable error when any string argument embeds newlines.

    Callers that expand an unquoted shell variable under zsh (no word splitting)
    pass a whole newline-joined name list as ONE argument — the 2026-07 usage
    audit traced ~all production CLI errors to this shape, surfacing only as the
    unhelpful "module not indexed" / "Symbol not found".

    Args:
        args: the parsed argparse namespace for this invocation.
    """
    for value in vars(args).values():
        if isinstance(value, str) and "\n" in value:
            names = [line for line in value.splitlines() if line.strip()]
            _exit_error(
                f"{len(names)} names passed as ONE argument (newline-joined) — your shell did not "
                "word-split the variable (zsh default). Call scan-query once per name, or use "
                "'batch' with one request item per name."
            )


def _exit_symbol_not_found(index: dict, qname: str) -> None:
    """Exit with the fn-* not-found error, hinting when *qname* is really a module.

    The 2026-07 usage audit found every bare-module ``fn-rdeps``/``fn-blast`` call
    failing with the generic "Symbol not found" — callers then retried other
    wrong shapes. Detecting the module case turns a dead end into a redirect.

    Args:
        index: parsed codemap index dict.
        qname: the symbol argument that failed to resolve.
    """
    if "::" not in qname and any(m.get("name") == qname for m in index.get("modules", [])):
        _exit_error(
            f"'{qname}' is a module, not a function qname — fn-* commands need 'module::function' "
            f"(see 'symbols {qname}' for its functions). For module-level callers use: rdeps {qname}"
        )
    _exit_error(f"Symbol '{qname}' not found. Use 'find-symbol <pattern>' to search.")


def cmd_fn_deps(index: dict, qname: str) -> None:
    """List the functions called by a qualified function name.

    Args:
        index: parsed codemap index dict (must be v3 with call graph).
        qname: fully qualified symbol name (``module::symbol``).
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    if qname not in sym_map:
        _exit_symbol_not_found(index, qname)
    _module_entry, sym = sym_map[qname]
    calls = [
        {"target": edge["target"], "resolution": edge.get("resolution", "")}
        for edge in sym.get("calls", [])
        if edge.get("resolution") in VALID_CALL_RESOLUTIONS
    ]
    _print(
        json.dumps(
            {
                "qname": qname,
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

    Args:
        index: parsed codemap index dict (must be v3 with call graph).
        qname: fully qualified symbol name (``module::symbol``).
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    resolved_qname = _resolve_symbol_alias(index, qname)
    if resolved_qname is None or (qname not in sym_map and qname not in (index.get("symbol_aliases") or {})):
        _exit_symbol_not_found(index, qname)
    rev_graph = _get_rev_graph(index)
    callers = []
    for caller_qname in sorted(set(rev_graph.get(resolved_qname, []))):
        entry = sym_map.get(caller_qname)
        m_entry = entry[0] if entry else {}
        callers.append(
            {
                "caller": caller_qname,
                "module": m_entry.get("name", ""),
                "path": m_entry.get("path", ""),
            }
        )
    if exclude_tests:
        callers = [c for c in callers if not sym_map.get(c["caller"], ({}, {}))[0].get("is_test")]
    fn_name = qname.split("::")[-1] if "::" in qname else qname
    _print(
        json.dumps(
            {
                "qname": qname,
                "resolved_qname": resolved_qname,
                "called_by": callers,
                "count": len(callers),
                "unique_caller_count": len(callers),
                "index": _cmd_coverage(
                    index,
                    query_target=qname,
                    method="static-ast",
                    not_covered=_CALL_GRAPH_NOT_COVERED,
                    hint=f'grep -rn "{fn_name}" to find hook-registered callers not in static AST',
                ),
            }
        )
    )


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

    Args:
        index: parsed codemap index dict (must be v3 with call graph).
        qname: fully qualified symbol name (``module::symbol``) to trace callers from.
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    if qname not in sym_map:
        _exit_symbol_not_found(index, qname)
    rev_graph = _get_rev_graph(index)

    # BFS
    visited: set[str] = {qname}
    queue: deque[tuple[str, int]] = deque([(qname, 0)])
    result = []
    while queue:
        node, depth = queue.popleft()
        for caller in rev_graph.get(node, []):
            if caller not in visited:
                visited.add(caller)
                entry = sym_map.get(caller)
                m_entry = entry[0] if entry else {}
                result.append(
                    {
                        "caller": caller,
                        "depth": depth + 1,
                        "module": m_entry.get("name", ""),
                        "path": m_entry.get("path", ""),
                    }
                )
                queue.append((caller, depth + 1))

    result.sort(key=lambda x: (x["depth"], x["caller"]))
    _print(
        json.dumps(
            {
                "qname": qname,
                "blast_radius": result,
                "total_callers": len(result),
                "index": _cmd_coverage(
                    index,
                    query_target=qname,
                    method="static-ast",
                    scope="transitive-call-graph",
                    not_covered=_CALL_GRAPH_NOT_COVERED,
                ),
            }
        )
    )


def _test_impact_via_function_call(index: dict, qname: str) -> tuple[set[str], set[str]]:
    """BFS the reverse call graph from *qname*; return (test files, test modules) reached.

    Args:
        index: parsed codemap index dict (must be v3+ with call graph).
        qname: ``module::symbol`` whose callers are traced transitively.
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    if qname not in sym_map:
        _exit_symbol_not_found(index, qname)
    rev_graph = _get_rev_graph(index)
    test_files: set[str] = set()
    test_mods: set[str] = set()
    visited: set[str] = {qname}
    queue: deque[str] = deque([qname])
    while queue:
        node = queue.popleft()
        for caller in rev_graph.get(node, []):
            if caller not in visited:
                visited.add(caller)
                entry = sym_map.get(caller)
                if entry:
                    m_entry = entry[0]
                    if m_entry.get("is_test"):
                        if p := m_entry.get("path", ""):
                            test_files.add(p)
                        if n := m_entry.get("name", ""):
                            test_mods.add(n)
                queue.append(caller)
    return test_files, test_mods


def _test_impact_via_module_import(index: dict, module_map: dict, qname: str) -> tuple[set[str], set[str]]:
    """BFS the reverse import graph from *qname*; return (test files, test modules) reached.

    Args:
        index: parsed codemap index dict.
        module_map: name-keyed module lookup from :func:`build_module_map`.
        qname: bare dotted module name whose importers are traced transitively.
    """
    if qname not in module_map:
        _exit_error(f"Module '{qname}' not found in index.")
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
    if "::" in qname:
        test_files_via_call, test_mods_via_call = _test_impact_via_function_call(index, qname)
    else:
        module_map = build_module_map(index)
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
    _print(
        json.dumps(
            {
                "qname": qname,
                "test_files": all_files,
                "test_modules": all_mods,
                "via_call": len(test_files_via_call),
                "via_mock": len(test_files_via_mock),
                "total": len(all_files),
                "pytest_cmd": pytest_cmd,
                "index": _cmd_coverage(
                    index,
                    query_target=qname,
                    method=method,
                    scope="test-impact",
                    not_covered=_CALL_GRAPH_NOT_COVERED,
                    hint=f'grep -rn "{short_name}" tests/ to find hook-based test deps not in static graph',
                ),
            }
        )
    )


# ---------------------------------------------------------------------------
# Mock reverse-dependency command (v4.1)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Subprocess call-edge commands (v5.2)
# ---------------------------------------------------------------------------


def _require_subprocess_rdep_count(index: dict) -> dict:
    """Exit with a clear error when the index lacks the v5.2 ``subprocess_rdep_count`` table.

    Fail-closed: a missing ``subprocess_rdep_count`` must never be silently
    treated as "no subprocess callers" — that would hide real edges. The caller
    is required to rebuild the index against v5.2+ first.

    Args:
        index: parsed codemap index dict.
    """
    if "subprocess_rdep_count" not in index:
        _exit_error("subprocess_rdep_count absent — rebuild with scan-index v5.2+")
    return index["subprocess_rdep_count"]


def cmd_subprocess_deps(index: dict, module: str) -> None:
    """Print every subprocess call made by *module* (forward edges).

    Lists ``{target_module, file, line}`` entries recorded by ``scan-index``
    for ``subprocess.run``, ``subprocess.Popen``, and ``os.system`` invocations
    that resolve to an indexed module.

    Args:
        index: parsed codemap index dict (must be v5.2+ with subprocess_calls).
        module: dotted module name whose forward subprocess edges are queried.
    """
    _require_feature(index, SUBPROCESS_CALLS_MIN_VER, "subprocess-deps")
    modules = build_module_map(index)
    entry = modules.get(module)
    if entry is None:
        _die_module_not_indexed(index, module)
    calls = entry.get("subprocess_calls", []) or []
    _print(
        json.dumps(
            {
                "module": module,
                "calls": calls,
                "count": len(calls),
                "index": _cmd_coverage(index, method="ast-flags", scope="subprocess-call-strings"),
            }
        )
    )


def cmd_subprocess_rdeps(index: dict, module: str) -> None:
    """Print every module that spawns *module* as a subprocess (reverse edges).

    Walks every indexed module's ``subprocess_calls`` list and collects entries
    whose ``target_module`` equals *module*. Fail-closed via
    :func:`_require_subprocess_rdep_count` — a missing reverse table aborts
    rather than silently reporting zero callers.

    Args:
        index: parsed codemap index dict (must be v5.2+ with subprocess_rdep_count).
        module: dotted module name whose reverse subprocess edges are queried.
    """
    _require_feature(index, SUBPROCESS_CALLS_MIN_VER, "subprocess-rdeps")
    _require_subprocess_rdep_count(index)
    callers: list[dict] = []
    for m in index.get("modules", []):
        for call in m.get("subprocess_calls", []) or []:
            if call.get("target_module") == module:
                callers.append(
                    {
                        "caller": m.get("name", ""),
                        "file": call.get("file", ""),
                        "line": call.get("line", 0),
                    }
                )
    callers.sort(key=lambda c: (c["caller"], c["file"], c["line"]))
    _print(
        json.dumps(
            {
                "module": module,
                "callers": callers,
                "count": len(callers),
                "index": _cmd_coverage(index, method="ast-flags", scope="subprocess-call-strings"),
            }
        )
    )


# ---------------------------------------------------------------------------
# Pytest fixture graph commands (v5.3)
# ---------------------------------------------------------------------------


def _collect_fixture_definitions(index: dict) -> dict[str, dict]:
    """Build a global ``fixture_name -> {scope, defined_in, depends_on}`` lookup.

    Aggregates fixture definitions from every module's ``fixture_exports`` block.
    Each fixture's own parameter list (``params`` field, populated by
    :func:`extract_fixtures`) becomes its ``depends_on`` set — the fixture's
    own argument names are the fixtures it requests.

    Deeper-conftest-wins semantics are approximated by depth-sorting the conftest
    list (deeper paths overwrite shallower ones). Test-file fixtures override
    conftest fixtures of the same name.

    Args:
        index: parsed codemap index dict.

    Returns:
        Mapping ``fixture_name -> {"scope", "defined_in", "depends_on"}``.
    """

    def _depth(path: str) -> int:
        parts = Path(path).parts
        return max(len(parts) - 1, 0)

    conftests: list[dict] = []
    test_files: list[dict] = []
    for m in index.get("modules", []):
        if m.get("status") != "ok":
            continue
        path = m.get("path", "")
        basename = Path(path).name
        if basename == "conftest.py":
            conftests.append(m)
        elif m.get("is_test"):
            test_files.append(m)
    conftests.sort(key=lambda m: _depth(m.get("path", "")))

    aggregated: dict[str, dict] = {}
    for m in (*conftests, *test_files):
        for fix in m.get("fixture_exports", []) or []:
            aggregated[fix["name"]] = {
                "scope": fix.get("scope", "function"),
                "defined_in": m.get("name", ""),
                "depends_on": list(fix.get("params", []) or []),
            }
    return aggregated


def cmd_fixture_rdeps(index: dict, fixture_name: str) -> None:
    """Print test files that use *fixture_name* anywhere in their test functions.

    Walks every test module's ``fixture_uses`` list and collects modules whose
    test functions take *fixture_name* as a parameter. Results include the
    fixture's resolved scope and defining module when known.

    Args:
        index: parsed codemap index dict (must be v5.3+ with ``fixture_uses``).
        fixture_name: name of the fixture whose reverse-dependencies are queried.
    """
    _require_feature(index, FIXTURE_GRAPH_MIN_VER, "fixture-rdeps")
    test_files: list[dict] = []
    for m in index.get("modules", []):
        if m.get("status") != "ok" or not m.get("is_test"):
            continue
        for fix in m.get("fixture_uses", []) or []:
            if fix.get("name") != fixture_name:
                continue
            test_files.append(
                {
                    "module": m.get("name", ""),
                    "path": m.get("path", ""),
                    "scope": fix.get("scope"),
                    "defined_in": fix.get("defined_in"),
                }
            )
            break
    test_files.sort(key=lambda e: (e["module"], e["path"]))
    _print(
        json.dumps(
            {
                "fixture": fixture_name,
                "count": len(test_files),
                "test_files": test_files,
                "index": _cmd_coverage(index, method="ast-flags", scope="pytest-fixture-deps"),
            }
        )
    )


_FIXTURE_GRAPH_MAX_DEPTH = 10


def _build_fixture_subtree(
    fixture_name: str,
    fixture_defs: dict[str, dict],
    visited: set[str],
    depth: int,
) -> dict:
    """Recursively expand the dependency tree of *fixture_name*, bounded by depth and cycle guard.

    Returns a node dict with ``name``, ``scope``, ``defined_in``, and ``depends_on``
    (a list of further node dicts). Cycle detection: a fixture already in
    *visited* is emitted with empty ``depends_on`` and a ``cycle: True`` marker.
    Depth cap: at :data:`_FIXTURE_GRAPH_MAX_DEPTH` recursion stops and the node
    is marked ``truncated: True``.

    Args:
        fixture_name: fixture to expand at this level.
        fixture_defs: global fixture lookup from :func:`_collect_fixture_definitions`.
        visited: names of fixtures already expanded on the current path (mutated).
        depth: current recursion depth.
    """
    info = fixture_defs.get(fixture_name)
    if info is None:
        return {
            "name": fixture_name,
            "scope": None,
            "defined_in": None,
            "depends_on": [],
        }
    node: dict = {
        "name": fixture_name,
        "scope": info.get("scope"),
        "defined_in": info.get("defined_in"),
    }
    if fixture_name in visited:
        node["depends_on"] = []
        node["cycle"] = True
        return node
    if depth >= _FIXTURE_GRAPH_MAX_DEPTH:
        node["depends_on"] = []
        node["truncated"] = True
        return node
    visited.add(fixture_name)
    children: list[dict] = []
    for dep in info.get("depends_on", []) or []:
        children.append(_build_fixture_subtree(dep, fixture_defs, visited, depth + 1))
    visited.remove(fixture_name)
    node["depends_on"] = children
    return node


def _find_test_module(index: dict, query: str) -> dict | None:
    """Locate a test module by dotted name or file-path suffix.

    Resolution order: exact ``name`` match, exact ``path`` match, then path
    suffix match. Only modules with ``status == "ok"`` and ``is_test == True``
    are considered.

    Args:
        index: parsed codemap index dict.
        query: dotted module name (``tests.foo``) or path (``tests/foo.py``).
    """
    for m in index.get("modules", []):
        if m.get("status") != "ok" or not m.get("is_test"):
            continue
        if m.get("name") == query or m.get("path") == query:
            return m
    for m in index.get("modules", []):
        if m.get("status") != "ok" or not m.get("is_test"):
            continue
        path = m.get("path", "")
        if path.endswith(query):
            return m
    return None


def cmd_fixture_graph(index: dict, test_file: str) -> None:
    """Print the full fixture dependency tree for *test_file*.

    Resolves *test_file* via :func:`_find_test_module`, then expands each
    fixture the test module uses into a recursive ``depends_on`` tree using
    :func:`_build_fixture_subtree`. Depth is capped at
    :data:`_FIXTURE_GRAPH_MAX_DEPTH` and cycles are surfaced via a
    ``cycle: True`` marker rather than raising.

    Args:
        index: parsed codemap index dict (must be v5.3+ with ``fixture_uses``).
        test_file: dotted module name or path identifying the test module.
    """
    _require_feature(index, FIXTURE_GRAPH_MIN_VER, "fixture-graph")
    module_entry = _find_test_module(index, test_file)
    if module_entry is None:
        _exit_error(f"Test module '{test_file}' not found in index.")
    fixture_defs = _collect_fixture_definitions(index)
    roots: list[dict] = []
    for fix in module_entry.get("fixture_uses", []) or []:
        visited: set[str] = set()
        roots.append(_build_fixture_subtree(fix["name"], fixture_defs, visited, 0))
    _print(
        json.dumps(
            {
                "test_file": module_entry.get("name", ""),
                "path": module_entry.get("path", ""),
                "fixtures": roots,
                "count": len(roots),
                "index": _cmd_coverage(index, method="ast-flags", scope="pytest-fixture-deps"),
            }
        )
    )


# ---------------------------------------------------------------------------
# Docstring coverage command (v4.4)
# ---------------------------------------------------------------------------


def _is_public_symbol(name: str) -> bool:
    """Return True when *name* is a public identifier (no leading underscore).

    Dunder names like ``__init__`` start with an underscore and are excluded —
    this matches the simplest rule consistent with "no leading ``_``" filtering.

    Examples:
        >>> _is_public_symbol("foo")
        True
        >>> _is_public_symbol("_helper")
        False
        >>> _is_public_symbol("__init__")
        False
        >>> _is_public_symbol("MyClass.method")
        True
        >>> _is_public_symbol("MyClass._priv")
        False
    """
    if not name:
        return False
    for part in name.split("."):
        if not part or part.startswith("_"):
            return False
    return True


def _symbol_loc(sym: dict) -> int:
    """Return the (end_line − start_line) span of a symbol; 0 when either is missing.

    Examples:
        >>> _symbol_loc({"start_line": 10, "end_line": 25})
        15
        >>> _symbol_loc({"start_line": 5})
        0
        >>> _symbol_loc({})
        0
    """
    start = sym.get("start_line")
    end = sym.get("end_line")
    if not isinstance(start, int) or not isinstance(end, int):
        return 0
    return end - start


def cmd_undocumented(index: dict, module: str | None, all_modules: bool) -> None:
    """List public symbols missing a docstring, sorted by LOC descending.

    Public symbol = no component of ``qualified_name`` starts with ``_`` —
    excludes dunders (``__init__``), private helpers (``_compute``), and private
    class names (``_Cache``). Test modules (``is_test=True``) are always skipped.

    Args:
        index: parsed codemap index dict (must be v4.4+ with ``has_docstring``).
        module: when set, restrict scan to this dotted module name only.
        all_modules: when True, scan every non-test module in the index.

    Examples:
        See ``tests/test_scan_query.py::TestDocstringCoverage`` for end-to-end coverage.
    """
    _require_feature(index, DOCSTRING_MIN_VER, "has_docstring")
    modules = index.get("modules", [])
    if module is not None:
        modules = [m for m in modules if m.get("name") == module]
    else:
        modules = [m for m in modules if not m.get("is_test")]
    # all_modules is the default and a no-op flag; preserved for explicit CLI clarity.
    _ = all_modules

    findings: list[dict] = []
    for m in modules:
        if m.get("status") == "degraded":
            continue
        mod_name = m.get("name", "")
        for sym in m.get("symbols", []):
            if sym.get("has_docstring", False):
                continue
            if not _is_public_symbol(sym.get("qualified_name", "")):
                continue
            findings.append(
                {
                    "name": sym.get("name", ""),
                    "qualified_name": sym.get("qualified_name", ""),
                    "module": mod_name,
                    "type": sym.get("type", ""),
                    "loc": _symbol_loc(sym),
                    "start_line": sym.get("start_line", 0),
                    "end_line": sym.get("end_line", 0),
                    "docstring_first_line": sym.get("docstring_first_line"),
                }
            )

    findings.sort(key=lambda f: (-f["loc"], f["module"], f["qualified_name"]))
    unique_qualified_names = sorted({finding["qualified_name"] for finding in findings})
    payload: dict = {
        "undocumented": findings,
        "total": len(findings),
        "unique_total": len(unique_qualified_names),
        "unique_qualified_names": unique_qualified_names,
        "count_semantics": {
            "total": "Undocumented public symbol declarations. Multiple declarations may share one qualified name.",
            "unique_total": "Unique qualified names among undocumented public symbol declarations.",
        },
        "index": _cmd_coverage(
            index, method="ast-flags", scope="public-api-only", excludes=["private", "dunder", "test-modules"]
        ),
    }
    if module is not None:
        payload["module"] = module
    _print(json.dumps(payload))


# ---------------------------------------------------------------------------
# Test-coverage command (v4.2)
# ---------------------------------------------------------------------------


def _module_uncovered_candidates(m: dict) -> list[dict]:
    """Return this module's public symbols with zero test callers and zero mocks.

    Args:
        m: one module entry from the index. Degraded and test modules yield
            no candidates (the caller may also pre-filter these; the check
            is repeated here so this helper is safe to call on any module).
    """
    if m.get("status") == "degraded":
        return []
    if m.get("is_test", False):
        return []
    mod_name = m.get("name", "")
    findings: list[dict] = []
    for sym in m.get("symbols", []):
        qname = sym.get("qualified_name", "")
        if not _is_public_symbol(qname):
            continue
        if sym.get("fn_rdep_test_count", 0) != 0:
            continue
        if sym.get("mock_rdep_count", 0) != 0:
            continue
        findings.append(
            {
                "name": sym.get("name", ""),
                "module": mod_name,
                "qualified_name": qname,
                "loc": _symbol_loc(sym),
                "fn_rdep_test_count": sym.get("fn_rdep_test_count", 0),
                "mock_rdep_count": sym.get("mock_rdep_count", 0),
            }
        )
    return findings


class UncoveredSort(str, Enum):
    """Sort order for the ``uncovered`` query.

    Inherits str so CLI values map straight onto members.
    """

    LOC = "loc"
    NAME = "name"
    MODULE = "module"


def cmd_uncovered(index: dict, args: argparse.Namespace) -> None:
    """Print public symbols with no test coverage, sorted and capped to ``--top``.

    A symbol is uncovered when ALL of the following hold:
      * ``qualified_name`` is public per :func:`_is_public_symbol` (no leading
        ``_`` in any dotted component — excludes dunders, private helpers,
        private classes).
      * ``fn_rdep_test_count == 0`` — no caller in a test module reaches it.
      * ``mock_rdep_count == 0`` — no test mocks it via ``patch()``.

    Only non-test modules are scanned. ``fn_rdep_test_count`` and
    ``mock_rdep_count`` are stored fields (v4.1+); the query reads them
    directly without rebuilding the call graph.

    Args:
        index: parsed codemap index dict (must be v4.2+ with ``fn_rdep_test_count``).
        args: parsed argparse namespace exposing ``module`` (str | None),
            ``all_modules`` (bool), ``sort`` (an :class:`UncoveredSort` value),
            and ``top`` (int).

    Examples:
        See ``tests/test_scan_query.py::TestUncovered`` for end-to-end coverage.
    """
    _require_feature(index, UNCOVERED_MIN_VER, "fn_rdep_test_count")
    module: str | None = args.module
    all_modules: bool = args.all_modules
    if module is None and not all_modules:
        _exit_error("Pass a module name or --all to scan every non-test module.")
    # all_modules is the default and a no-op flag; preserved for explicit CLI clarity.
    _ = all_modules

    modules = index.get("modules", [])
    if module is not None:
        modules = [m for m in modules if m.get("name") == module]
    else:
        modules = [m for m in modules if not m.get("is_test")]

    findings = [f for m in modules for f in _module_uncovered_candidates(m)]

    sort_key = UncoveredSort(args.sort)
    if sort_key == UncoveredSort.NAME:
        findings.sort(key=lambda f: (f["qualified_name"], f["module"]))
    elif sort_key == UncoveredSort.MODULE:
        findings.sort(key=lambda f: (f["module"], f["qualified_name"]))
    else:  # UncoveredSort.LOC — default
        findings.sort(key=lambda f: (-f["loc"], f["module"], f["qualified_name"]))

    total = len(findings)
    unique_qualified_names = sorted({finding["qualified_name"] for finding in findings})
    top_n = max(0, int(args.top))
    showing = min(total, top_n)
    findings = findings[:top_n]

    payload: dict = {
        "uncovered": findings,
        "total": total,
        "showing": showing,
        "unique_total": len(unique_qualified_names),
        "unique_qualified_names": unique_qualified_names,
        "count_semantics": {
            "definition": "Public symbols with zero test callers and zero mocks.",
            "total": "All matching static public symbol declarations before the --top display cap.",
            "showing": "Number of matching declarations included in uncovered after the --top display cap.",
            "unique_total": "Unique qualified names among all matching static public symbol declarations.",
        },
        "index": _cmd_coverage(index, method="ast-flags", scope="public-api-only", excludes=["private", "dunder"]),
    }
    if module is not None:
        payload["module"] = module
    _print(json.dumps(payload))


# ---------------------------------------------------------------------------
# Line-coverage commands (v5.4)
# ---------------------------------------------------------------------------


def _split_coverage_qname(qname: str) -> tuple[str, str | None]:
    """Split a ``module::symbol`` query into ``(module, symbol)``; bare module → ``(module, None)``.

    Examples:
        >>> _split_coverage_qname("pkg.mod::func")
        ('pkg.mod', 'func')
        >>> _split_coverage_qname("pkg.mod")
        ('pkg.mod', None)
        >>> _split_coverage_qname("pkg.mod::Cls.method")
        ('pkg.mod', 'Cls.method')
    """
    if "::" in qname:
        module, symbol = qname.split("::", 1)
        return module, symbol
    return qname, None


def _find_module(index: dict, module_name: str) -> dict | None:
    """Return the module entry whose ``name`` matches *module_name*, or ``None``."""
    for m in index.get("modules", []):
        if m.get("name") == module_name:
            return m
    return None


def cmd_coverage(index: dict, qname: str) -> None:
    """Show ``coverage_pct`` and ``covered_by`` for a specific symbol or whole module.

    Accepts two query shapes:

      * ``module::symbol`` — return one symbol's coverage fields, or an explicit
        error JSON when the symbol is not present in the index or its coverage
        fields were not populated (index built without ``--with-coverage``).
      * ``module`` — return the per-symbol coverage map for every symbol in the
        module that has coverage data attached.

    Args:
        index: parsed codemap index dict (must be v5.4+).
        qname: ``module::symbol`` query string, or a bare ``module`` name.
    """
    _require_feature(index, COVERAGE_MIN_VER, "coverage")
    module_name, symbol_name = _split_coverage_qname(qname)
    module = _find_module(index, module_name)
    if module is None:
        _die_module_not_indexed(index, module_name)
    if symbol_name is None:
        rows: list[dict] = []
        for sym in module.get("symbols", []):
            if "coverage_pct" not in sym:
                continue
            rows.append(
                {
                    "qualified_name": sym.get("qualified_name", ""),
                    "type": sym.get("type", ""),
                    "coverage_pct": sym.get("coverage_pct"),
                    "covered_by": sym.get("covered_by"),
                    "start_line": sym.get("start_line", 0),
                    "end_line": sym.get("end_line", 0),
                }
            )
        _print(
            json.dumps(
                {
                    "module": module_name,
                    "symbols": rows,
                    "total": len(rows),
                    "index": _cmd_coverage(index, method="ast-flags", scope="line-coverage"),
                }
            )
        )
        return

    for sym in module.get("symbols", []):
        if sym.get("qualified_name") != symbol_name:
            continue
        if "coverage_pct" not in sym:
            _exit_error(
                f"Symbol '{module_name}::{symbol_name}' has no coverage data — "
                "rebuild the index with `scan-index --with-coverage <path>`."
            )
        _print(
            json.dumps(
                {
                    "module": module_name,
                    "qualified_name": symbol_name,
                    "type": sym.get("type", ""),
                    "coverage_pct": sym.get("coverage_pct"),
                    "covered_by": sym.get("covered_by"),
                    "start_line": sym.get("start_line", 0),
                    "end_line": sym.get("end_line", 0),
                    "index": _cmd_coverage(index, method="ast-flags", scope="line-coverage"),
                }
            )
        )
        return
    _exit_error(f"Symbol '{module_name}::{symbol_name}' not found in module.")


def _module_coverage_gap_candidates(m: dict, threshold: float) -> list[dict]:
    """Return this module's public symbols whose ``coverage_pct`` is strictly below *threshold*.

    Args:
        m: one module entry from the index (degraded/test modules yield nothing).
        threshold: lower bound on acceptable coverage.
    """
    if m.get("status") == "degraded":
        return []
    if m.get("is_test", False):
        return []
    mod_name = m.get("name", "")
    findings: list[dict] = []
    for sym in m.get("symbols", []):
        qname = sym.get("qualified_name", "")
        if not _is_public_symbol(qname):
            continue
        pct = sym.get("coverage_pct")
        if pct is None:
            continue
        if pct >= threshold:
            continue
        findings.append(
            {
                "module": mod_name,
                "qualified_name": qname,
                "type": sym.get("type", ""),
                "coverage_pct": pct,
                "gap": round(threshold - pct, 4),
                "start_line": sym.get("start_line", 0),
                "end_line": sym.get("end_line", 0),
            }
        )
    return findings


def cmd_coverage_gap(
    index: dict,
    module: str | None,
    all_modules: bool,
    threshold: float,
) -> None:
    """List public symbols whose ``coverage_pct`` is strictly below *threshold*.

    Findings are sorted by ``gap = threshold - coverage_pct`` descending so the
    largest-coverage holes appear first. Only ``status == "ok"`` modules are
    scanned; test modules are skipped (matching ``cmd_uncovered``). Symbols
    without ``coverage_pct`` (module not covered, or index built without
    ``--with-coverage``) are silently ignored — they belong to ``cmd_uncovered``.

    Args:
        index: parsed codemap index dict (must be v5.4+).
        module: when set, restrict scan to this dotted module name only.
        all_modules: when True, scan every non-test module in the index.
        threshold: lower bound on acceptable coverage (default 0.8 set in argparse).
    """
    _require_feature(index, COVERAGE_MIN_VER, "coverage-gap")
    if module is None and not all_modules:
        _exit_error("Pass a module name or --all to scan every non-test module.")
    _ = all_modules  # all_modules is the default and a no-op flag; preserved for explicit CLI clarity.

    modules = index.get("modules", [])
    if module is not None:
        modules = [m for m in modules if m.get("name") == module]
    else:
        modules = [m for m in modules if not m.get("is_test")]

    findings = [f for m in modules for f in _module_coverage_gap_candidates(m, threshold)]

    findings.sort(key=lambda f: (-f["gap"], f["module"], f["qualified_name"]))
    payload: dict = {
        "coverage_gap": findings,
        "total": len(findings),
        "threshold": threshold,
        "index": _cmd_coverage(index, method="ast-flags", scope="line-coverage-gap"),
    }
    if module is not None:
        payload["module"] = module
    _print(json.dumps(payload))


# ---------------------------------------------------------------------------
# Sphinx / MkDocs xrefs command (v4.5)
# ---------------------------------------------------------------------------


def _iter_all_xrefs(index: dict):
    """Yield every xref entry in the index — module docstrings and doc files combined.

    Args:
        index: parsed codemap index dict (v4.5+).

    Yields:
        Individual xref dicts as recorded by ``scan-index``.
    """
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        for entry in m.get("sphinx_xrefs", []) or []:
            yield entry
    for entry in index.get("doc_xrefs", []) or []:
        yield entry


# Roles whose targets resolve to ``module::name`` symbol keys (must match
# scan-index ``_SPHINX_RESOLVABLE_ROLES`` minus ``mod``/``attr``/``data``).
# ``mod`` stores bare module names; ``attr``/``data`` are best-effort and may
# legitimately point at non-symbol identifiers (instance attributes, runtime
# globals) — excluding them from the broken check avoids false positives.
_SYMBOL_ROLES: frozenset[str] = frozenset({"func", "class", "meth", "exc", "mkdocs"})


def cmd_xrefs(index: dict, query: str, broken: bool) -> None:
    """List doc cross-references targeting *query*, or surface broken refs.

    Two modes (selected by *broken*):
      * **Default** — every recorded xref whose ``target`` equals *query*. The
        returned list pools module-docstring refs and ``.rst``/``.md`` doc refs.
      * **``--broken``** — *query* names a module; every xref whose target's
        module prefix matches *query* and whose target is not present in the
        symbol index is reported. ``attr``/``data``/``mod`` refs are skipped
        because they may legitimately point at non-symbol identifiers.

    Args:
        index: parsed codemap index dict (must be v4.5+ with ``sphinx_xrefs``).
        query: symbol qname (default mode) or module name (``--broken`` mode).
        broken: when True, switch to broken-ref discovery mode.
    """
    _require_feature(index, SPHINX_XREFS_MIN_VER, "sphinx_xrefs")

    if not broken:
        refs: list[dict] = []
        for entry in _iter_all_xrefs(index):
            if entry.get("target") == query:
                refs.append(
                    {
                        "role": entry.get("role", ""),
                        "file": entry.get("file", ""),
                        "line": entry.get("line", 0),
                        "source": entry.get("source", ""),
                    }
                )
        refs.sort(key=lambda r: (r["file"], r["line"], r["role"]))
        _print(
            json.dumps(
                {
                    "target": query,
                    "refs": refs,
                    "count": len(refs),
                    "index": _cmd_coverage(index, method="ast-flags", scope="sphinx-xrefs"),
                }
            )
        )
        return

    # ``--broken`` mode: scan refs whose target module matches *query* (or all if query == "").
    sym_map = _get_symbol_map(index)
    module_prefix = f"{query}::" if query else ""
    broken_refs: list[dict] = []
    seen: set[tuple[str, str, int, str]] = set()  # (target, file, line, role) — collapse module/doc duplicates
    for entry in _iter_all_xrefs(index):
        target = entry.get("target", "")
        role = entry.get("role", "")
        if role not in _SYMBOL_ROLES:
            continue
        if module_prefix and not target.startswith(module_prefix):
            continue
        if target in sym_map:
            continue
        sig = (target, entry.get("file", ""), entry.get("line", 0), role)
        if sig in seen:
            continue
        seen.add(sig)
        broken_refs.append(
            {
                "target": target,
                "role": role,
                "file": entry.get("file", ""),
                "line": entry.get("line", 0),
                "source": entry.get("source", ""),
            }
        )
    broken_refs.sort(key=lambda r: (r["target"], r["file"], r["line"]))
    _print(
        json.dumps(
            {
                "module": query,
                "broken": broken_refs,
                "count": len(broken_refs),
                "index": _cmd_coverage(index, method="ast-flags", scope="sphinx-xrefs"),
            }
        )
    )


# ---------------------------------------------------------------------------
# Dead-symbol / dead-module commands (v4.6)
# ---------------------------------------------------------------------------


def _require_sphinx_xref_count(index: dict) -> None:
    """Exit with a clear error when the index lacks the v4.5 ``sphinx_xref_count`` table.

    Fail-closed: a missing ``sphinx_xref_count`` must never be silently treated
    as "no doc references" — that would mark documented symbols as dead. The
    caller is required to rebuild the index against v4.5+ first.

    Args:
        index: parsed codemap index dict.
    """
    if "sphinx_xref_count" not in index:
        _exit_error("v4.5 sphinx index required — rebuild with scan-index after v4.5 ships (sphinx_xref_count missing)")


def _fn_rdep_count(qname: str, rev_graph: dict[str, list[str]]) -> int:
    """Return the number of distinct function-level callers of *qname*.

    Args:
        qname: fully-qualified symbol key ``module::symbol``.
        rev_graph: reverse call graph from :func:`build_reverse_call_graph`.

    Examples:
        >>> _fn_rdep_count("m::f", {"m::f": ["m::a", "m::b"]})
        2
        >>> _fn_rdep_count("m::missing", {})
        0
    """
    return len(rev_graph.get(qname, []))


def cmd_dead_symbols(index: dict, args: argparse.Namespace) -> None:
    """Print public symbols with zero callers anywhere in the project.

    A symbol qualifies as "dead" when **every** signal is zero:

      * ``rdep_count == 0`` on the owning module — no static importer
      * ``fn_rdep_count == 0`` — no function-level caller (reverse call graph)
      * ``mock_rdep_count == 0`` — not mocked by any test
      * ``sphinx_xref_count[qname] == 0`` — not referenced in any docstring,
        ``.rst``, or mkdocstrings file
      * ``qualified_name`` is public (no ``_`` prefix on any component)
      * Owning module is **not** an entry-point (``if __name__ == "__main__"``)
      * Owning module is **not** a test file
      * Symbol name is **not** in the module's ``__all__`` list when present

    Modules with ``has_star_imports=True`` are skipped entirely — star imports
    prevent reliable call-graph tracing; a warning per skipped module is logged
    to stderr.

    The ``sphinx_xref_count`` table is required (fail-closed): a missing key
    aborts the command rather than silently treating documented symbols as dead.

    Output is sorted by LOC descending — biggest dead symbol first.

    Args:
        index: parsed codemap index dict (must be v4.6+).
        args: parsed argparse namespace exposing ``min_loc`` (int, default 5).
    """
    _require_feature(index, DEAD_SYMBOL_MIN_VER, "dead-symbol")
    _require_sphinx_xref_count(index)
    min_loc: int = int(args.min_loc)
    sphinx_counts: dict[str, int] = index.get("sphinx_xref_count", {})
    rev_graph = _get_rev_graph(index)

    eligible, skipped_star = _dead_symbol_eligible_modules(index)
    findings = [f for m in eligible for f in _module_dead_symbol_candidates(m, min_loc, rev_graph, sphinx_counts)]

    findings.sort(key=lambda f: (-f["loc"], f["module"], f["qualified_name"]))
    _print(
        json.dumps(
            {
                "dead": findings,
                "total": len(findings),
                "skipped_star_import": skipped_star,
                "index": _cmd_coverage(index, method="static-ast", scope="dead-symbols"),
            }
        )
    )


def _dead_symbol_eligible_modules(index: dict) -> tuple[list[dict], list[str]]:
    """Return modules eligible for dead-symbol scanning, plus star-import skips.

    A module is eligible when it is not degraded, not a test file, not an
    entry-point, has no star imports, and has zero external importers
    (``rdep_count == 0`` — a module still imported elsewhere can't have a
    truly dead symbol, since the import graph doesn't prove the symbol
    itself is unused). Star-import modules are skipped (call-graph tracing
    through ``from x import *`` is unreliable) with a per-module stderr
    warning; their names are also returned so the caller can report them.

    Args:
        index: parsed codemap index dict.
    """
    eligible: list[dict] = []
    skipped_star: list[str] = []
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        if m.get("is_test", False):
            continue
        if m.get("is_entry_point", False):
            continue
        if m.get("has_star_imports", False):
            mod_name = m.get("name", "")
            skipped_star.append(mod_name)
            _print(
                f"⚠ dead-symbol skipped {mod_name} — star imports prevent reliable call graph",
                file=sys.stderr,
            )
            continue
        if m.get("rdep_count", 0) != 0:
            continue
        eligible.append(m)
    return eligible, skipped_star


def _module_dead_symbol_candidates(
    m: dict, min_loc: int, rev_graph: dict[str, list[str]], sphinx_counts: dict[str, int]
) -> list[dict]:
    """Return this module's public symbols with zero callers/mocks/xrefs anywhere.

    Args:
        m: one module entry from the index (already filtered eligible by
            :func:`_dead_symbol_eligible_modules`).
        min_loc: skip symbols spanning fewer than this many lines.
        rev_graph: reverse call graph from :func:`_get_rev_graph`.
        sphinx_counts: the index's ``sphinx_xref_count`` table.
    """
    mod_name = m.get("name", "")
    exports: list[str] | None = m.get("exports")
    findings: list[dict] = []
    for sym in m.get("symbols", []):
        qname = sym.get("qualified_name", "")
        if not _is_public_symbol(qname):
            continue
        if exports is not None and sym.get("name", "") in exports:
            continue
        loc = _symbol_loc(sym)
        if loc < min_loc:
            continue
        full_qname = f"{mod_name}::{qname}"
        fn_rdeps = _fn_rdep_count(full_qname, rev_graph)
        if fn_rdeps != 0:
            continue
        if sym.get("mock_rdep_count", 0) != 0:
            continue
        if sphinx_counts.get(full_qname, 0) != 0:
            continue
        findings.append(
            {
                "name": sym.get("name", ""),
                "module": mod_name,
                "qualified_name": qname,
                "loc": loc,
                "rdep_count": m.get("rdep_count", 0),
                "fn_rdep_count": fn_rdeps,
            }
        )
    return findings


def cmd_dead_modules(index: dict, args: argparse.Namespace) -> None:
    """Print modules with zero external importers (likely dead modules).

    A module qualifies as dead when:

      * ``rdep_count == 0`` — no other module imports it statically
      * ``is_entry_point == False`` — not a runnable ``__main__`` script
      * ``is_test == False`` — not a test file

    Dynamic importers (``dynamic_imported_by``) and config-file references
    (``config_refs``) are deliberately ignored: a module reachable only via
    dynamic dispatch is, by this definition, structurally dead from the static
    call graph's point of view. Callers needing dynamic awareness should use
    ``rdeps <module>`` to inspect the full reverse-import surface.

    Output is sorted by LOC descending — biggest dead module first.

    Args:
        index: parsed codemap index dict (must be v4.6+).
        args: parsed argparse namespace (no flags consumed today, reserved
            for future filtering options).
    """
    _require_feature(index, DEAD_SYMBOL_MIN_VER, "dead-symbol")
    _ = args  # placeholder — kept for future filtering knobs (e.g. ``--min-loc``)

    findings: list[dict] = []
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        if m.get("is_test", False):
            continue
        if m.get("is_entry_point", False):
            continue
        if m.get("rdep_count", 0) != 0:
            continue
        findings.append(
            {
                "name": m.get("name", ""),
                "rdep_count": m.get("rdep_count", 0),
                "loc": m.get("loc", 0),
            }
        )

    findings.sort(key=lambda f: (-f["loc"], f["name"]))
    _print(
        json.dumps(
            {
                "dead_modules": findings,
                "total": len(findings),
                "index": _cmd_coverage(index, method="import-graph", scope="dead-modules"),
            }
        )
    )


# ---------------------------------------------------------------------------
# diff-impact: git diff → structural blast radius for the change set
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# batch: run N queries in one process, share the coverage block once
# ---------------------------------------------------------------------------


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
    global _capture  # noqa: PLW0603
    try:
        sub_args = parser.parse_args(argv)
    except SystemExit:
        return {"error": "invalid command or arguments"}, None
    buf: list[str] = []
    _capture = buf
    try:
        _dispatch_command(index, sub_args, parser, project_root)
    except SystemExit:
        # A handler hit _die_json / _exit_error; its JSON error object is already in
        # buf. Fall through to decode it as this sub-query's result.
        pass
    finally:
        _capture = None
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _add_module_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register module-level query subcommands: deps, rdeps, central, coupled, path, list, packages."""
    p_deps = sub.add_parser("deps", help="What does a module import?")
    p_deps.add_argument("module")
    p_deps.add_argument(
        "--stdlib",
        action="store_true",
        default=False,
        help="Restrict to stdlib imports only (requires v4.3+ index).",
    )
    p_deps.add_argument(
        "--third-party",
        action="store_true",
        default=False,
        help="Restrict to third-party imports only (requires v4.3+ index).",
    )
    p_deps.add_argument(
        "--internal",
        action="store_true",
        default=False,
        help="Restrict to internal (project-owned) imports only (requires v4.3+ index).",
    )

    p_rdeps = sub.add_parser("rdeps", help="What imports a module?")
    p_rdeps.add_argument("module")
    p_rdeps.add_argument("--exclude-tests", action="store_true", default=False, help="Exclude test files from results")
    p_rdeps.add_argument(
        "--entity",
        default=None,
        choices=[e.value for e in EntityType],
        help="Restrict importers to this entity type (requires v5.5+ index for docs/example).",
    )
    p_rdeps.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Preview at most N static importers; 0 returns every importer (default).",
    )

    p_central = sub.add_parser("central", help="Most-imported modules (highest blast radius).")
    p_central.add_argument("--top", type=int, default=10, metavar="N")
    p_central.add_argument(
        "--exclude-tests", action="store_true", default=False, help="Exclude test files from results"
    )
    p_central.add_argument(
        "--entity",
        default=None,
        choices=[e.value for e in EntityType],
        help="Restrict to this entity type (requires v5.5+ index for docs/example).",
    )

    coupled_help = "Modules ranked by internal import count (highest coupling)."
    p_coupled = sub.add_parser("coupled", help=coupled_help, description=coupled_help)
    p_coupled.add_argument("--top", type=int, default=10, metavar="N")
    p_coupled.add_argument(
        "--exclude-tests", action="store_true", default=False, help="Exclude test files from results"
    )
    p_coupled.add_argument(
        "--entity",
        default=None,
        choices=[e.value for e in EntityType],
        help="Restrict to this entity type (requires v5.5+ index for docs/example).",
    )

    p_path = sub.add_parser("path", help="Shortest import path between two modules.")
    p_path.add_argument("frm", metavar="from")
    p_path.add_argument("to")

    p_list = sub.add_parser("list", help="List all indexed modules.")
    p_list.add_argument(
        "--limit", type=int, default=100, metavar="N", help="Max modules to return (default 100). Use 0 for all."
    )

    sub.add_parser(
        "packages",
        help="Top-level packages with module/test/docs/example counts (requires v5.5+ index for docs/example).",
    )


def _add_symbol_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register symbol-level query subcommands: symbol, symbols, find-symbol."""
    p_symbol = sub.add_parser("symbol", help="Get source of a symbol by name (function/class/method).")
    p_symbol.add_argument("name", help="Symbol name, e.g. 'authenticate' or 'MyClass.method'")
    p_symbol.add_argument(
        "--limit", type=int, default=20, metavar="N", help="Max results (default 20). Use 0 for unlimited."
    )
    p_symbol.add_argument("--exclude-tests", action="store_true", default=False, help="Exclude test files from results")
    p_symbol.add_argument(
        "--with-imports",
        action="store_true",
        default=False,
        help="Include module-level import block alongside each symbol's source.",
    )

    p_symbols = sub.add_parser("symbols", help="List all symbols in a module.")
    p_symbols.add_argument("module", help="Dotted module name, e.g. 'mypackage.auth'")

    p_find = sub.add_parser("find-symbol", help="Regex search across all symbol names.")
    p_find.add_argument("pattern", help="Python regex pattern, e.g. 'auth' or '^My.*Handler$'")
    p_find.add_argument(
        "--limit", type=int, default=20, metavar="N", help="Max results (default 20). Use 0 for unlimited."
    )
    p_find.add_argument("--exclude-tests", action="store_true", default=False, help="Exclude test files from results")


def _add_callgraph_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register call-graph subcommands that require a version 3 or newer index.

    Registers ``fn-deps``, ``fn-rdeps``, ``fn-central``, ``fn-blast``, ``test-impact``, and ``mock-rdeps``.
    """
    p_fn_deps = sub.add_parser("fn-deps", help="What does a function call? (requires v3 index)")
    p_fn_deps.add_argument("qname", help="Full qname: module::symbol, e.g. 'mypackage.auth::validate_token'")

    p_fn_rdeps = sub.add_parser("fn-rdeps", help="What calls a function? (requires v3 index)")
    p_fn_rdeps.add_argument("qname", help="Full qname: module::symbol")
    p_fn_rdeps.add_argument(
        "--exclude-tests", action="store_true", default=False, help="Exclude test files from results"
    )

    p_fn_central = sub.add_parser("fn-central", help="Most-called functions globally (requires v3 index)")
    p_fn_central.add_argument("--top", type=int, default=10, metavar="N")
    p_fn_central.add_argument(
        "--exclude-tests", action="store_true", default=False, help="Exclude test files from results"
    )

    p_fn_blast = sub.add_parser("fn-blast", help="Transitive reverse-call blast radius (requires v3 index)")
    p_fn_blast.add_argument("qname", help="Full qname: module::symbol")

    p_test_impact = sub.add_parser(
        "test-impact",
        help="Which tests are affected by changing a function or module? (requires v3+ index)",
    )
    p_test_impact.add_argument(
        "qname",
        help="module::symbol for function-level impact, or bare module name for module-level impact.",
    )
    p_test_impact.add_argument(
        "--no-mocks",
        dest="include_mocks",
        action="store_false",
        default=True,
        help="Exclude tests that only mock qname (no call/import path) from results.",
    )

    p_mock_rdeps = sub.add_parser(
        "mock-rdeps",
        help="Test files that mock a symbol via patch() (requires v4.1+ index)",
    )
    p_mock_rdeps.add_argument(
        "query",
        help="Full qname (module::symbol) for one symbol, or bare module for all mocked symbols in that module",
    )


def _add_subprocess_fixture_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register subprocess/fixture/import-group subcommands (require v4.3+/v5.2+/v5.3+ index)."""
    p_sub_deps = sub.add_parser(
        "subprocess-deps",
        help="What does this module spawn as a subprocess? (requires v5.2+ index)",
    )
    p_sub_deps.add_argument("module", help="Dotted module name whose subprocess calls are listed.")

    p_sub_rdeps = sub.add_parser(
        "subprocess-rdeps",
        help="What modules spawn this module as a subprocess? (requires v5.2+ index)",
    )
    p_sub_rdeps.add_argument("module", help="Dotted module name whose subprocess callers are listed.")

    p_fix_rdeps = sub.add_parser(
        "fixture-rdeps",
        help="Test files that use a pytest fixture (requires v5.3+ index).",
    )
    p_fix_rdeps.add_argument("fixture_name", help="Fixture name whose reverse-dependencies are queried.")

    p_fix_graph = sub.add_parser(
        "fixture-graph",
        help="Full pytest fixture dependency tree for a test file (requires v5.3+ index).",
    )
    p_fix_graph.add_argument(
        "test_file",
        help="Dotted test-module name (tests.foo) or path (tests/foo.py).",
    )

    p_import_types = sub.add_parser(
        "import-types",
        help="Return stdlib/third_party/internal import groups for a module (requires v4.3+ index).",
    )
    p_import_types.add_argument("module", help="Dotted module name, e.g. 'mypackage.auth'")


def _add_docs_coverage_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register docstring/test/line-coverage subcommands: undocumented, uncovered, coverage, coverage-gap."""
    p_undoc = sub.add_parser(
        "undocumented",
        help="List public symbols missing a docstring, sorted by LOC desc (requires v4.4+ index).",
    )
    p_undoc.add_argument(
        "module",
        nargs="?",
        default=None,
        help="Dotted module name to scan (omit and pass --all to scan every non-test module).",
    )
    p_undoc.add_argument(
        "--all",
        dest="all_modules",
        action="store_true",
        default=False,
        help="Scan all non-test modules in the index.",
    )

    p_uncov = sub.add_parser(
        "uncovered",
        help="Public symbols with no test callers and no mocks (requires v4.2+ index).",
    )
    p_uncov.add_argument(
        "module",
        nargs="?",
        default=None,
        help="Dotted module name to scan (omit and pass --all to scan every non-test module).",
    )
    p_uncov.add_argument(
        "--all",
        dest="all_modules",
        action="store_true",
        default=False,
        help="Scan all non-test modules in the index.",
    )
    p_uncov.add_argument(
        "--sort",
        choices=[k.value for k in UncoveredSort],
        default=UncoveredSort.LOC.value,
        help="Sort order: loc (default — biggest first), name (alphabetical), module (group by module).",
    )
    p_uncov.add_argument(
        "--top",
        type=int,
        default=20,
        metavar="N",
        help="Cap output to top N results (default 20).",
    )

    p_coverage = sub.add_parser(
        "coverage",
        help="Show coverage_pct and covered_by for a symbol or whole module (requires v5.4+ index).",
    )
    p_coverage.add_argument(
        "qname",
        help="Full qname (module::symbol) for one symbol, or bare module for every symbol in the module.",
    )

    p_cov_gap = sub.add_parser(
        "coverage-gap",
        help="Public symbols with coverage_pct below --threshold, sorted by gap desc (requires v5.4+ index).",
    )
    p_cov_gap.add_argument(
        "module",
        nargs="?",
        default=None,
        help="Dotted module name to scan (omit and pass --all to scan every non-test module).",
    )
    p_cov_gap.add_argument(
        "--all",
        dest="all_modules",
        action="store_true",
        default=False,
        help="Scan all non-test modules in the index.",
    )
    p_cov_gap.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        metavar="P",
        help="Coverage fraction (0.0–1.0) below which a symbol is reported (default 0.8).",
    )


def _add_xref_dead_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register doc-xref/dead-code subcommands: xrefs, dead-symbols, dead-modules."""
    p_xrefs = sub.add_parser(
        "xrefs",
        help="List doc cross-references for a symbol, or find broken refs (requires v4.5+ index).",
    )
    p_xrefs.add_argument(
        "query",
        help="Symbol qname (default mode) or module name (with --broken).",
    )
    p_xrefs.add_argument(
        "--broken",
        action="store_true",
        default=False,
        help="Find xrefs whose resolved target is not a known symbol in the index.",
    )

    p_dead_syms = sub.add_parser(
        "dead-symbols",
        help="Public symbols with zero callers anywhere (requires v4.6+ index).",
    )
    p_dead_syms.add_argument(
        "--min-loc",
        type=int,
        default=5,
        metavar="N",
        help="Skip symbols spanning fewer than N lines (default 5 — drops trivial properties).",
    )

    sub.add_parser(
        "dead-modules",
        help="Modules with zero external importers (requires v4.6+ index).",
    )


def _add_composite_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register composite subcommands that run their own in-process sub-queries: diff-impact, batch."""
    p_diff_impact = sub.add_parser(
        "diff-impact",
        help="Structural blast radius of the git change set: per-module rdeps/coupled, "
        "per-symbol fn-rdeps, unioned test-impact, risk tiers (requires v3+ index).",
    )
    p_diff_impact.add_argument(
        "--base",
        default="HEAD",
        metavar="REF",
        help="Git ref or range to diff against (default HEAD — staged + unstaged working-tree changes).",
    )
    p_diff_impact.add_argument(
        "--diff-file",
        default=None,
        metavar="PATH",
        help="Read the change set from a unified-diff file (e.g. `gh pr diff` output) instead of "
        "local git — for PR review where the change is not in the local object store. '-' reads stdin.",
    )

    p_batch = sub.add_parser(
        "batch",
        help="Run many queries in one process, sharing one coverage block (reads a JSON array).",
    )
    p_batch.add_argument(
        "input",
        nargs="?",
        default="-",
        help="A JSON array of [{cmd, args}] objects, a path to a file holding one, or '-' for stdin (default).",
    )


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    """Register flags shared by every query subcommand."""
    parser.add_argument("--index", metavar="PATH", help="Explicit path to the index JSON (auto-discovered if omitted).")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override project root for FILE-PATH RESOLUTION ONLY (does not re-scan or re-target the index; "
        "highest priority, supersedes scan_root and git root). Disagreeing with the index's scan_root flags "
        "root_mismatch and forces query_complete=false.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        metavar="N",
        help="Hard timeout in seconds; 0 = no limit (default). Uses SIGALRM — Unix only.",
    )
    parser.add_argument(
        "--no-heal",
        action="store_true",
        default=False,
        help="Disable the bounded incremental self-heal on a stale index (answer from the stale index as-is).",
    )
    parser.add_argument(
        "--verbose-coverage",
        action="store_true",
        default=False,
        help="Always emit the full coverage block, even after the first query of a session (disables the diet).",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        default=False,
        help="Emit compact coverage metadata and bounded alias-limitation evidence.",
    )


class _ScanQueryArgumentParser(argparse.ArgumentParser):
    """Preserve argparse failures while guiding common invalid scan-query commands."""

    def error(self, message: str) -> None:
        """Append one explicit migration hint to a known invalid subcommand error."""
        match = re.match(r"argument command: invalid choice: '([^']+)'", message)
        suggestion = ""
        if match:
            command = match.group(1)
            if command == "search":
                suggestion = "use 'find-symbol' to search symbols."
            elif command in {"callers", "find-references"}:
                suggestion = "use 'fn-rdeps' for function callers."
            elif command == "imports":
                suggestion = "use 'rdeps' for importers or 'deps' for imports."
            elif command == "help":
                suggestion = "use '--help' to list commands."
        if suggestion:
            message = f"{message}\n\nHint: {suggestion}"
        super().error(message)


def _build_parser() -> argparse.ArgumentParser:
    """Build the scan-query argument parser: every subcommand plus the shared global flags."""
    parser = _ScanQueryArgumentParser(
        description="Query the codemap structural index.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_module_subparsers(sub)
    _add_symbol_subparsers(sub)
    _add_callgraph_subparsers(sub)
    _add_subprocess_fixture_subparsers(sub)
    _add_docs_coverage_subparsers(sub)
    _add_xref_dead_subparsers(sub)
    _add_composite_subparsers(sub)
    _add_global_flags(parser)
    return parser


def _resolve_index_path(args: argparse.Namespace) -> Path:
    """Resolve the index JSON path from ``--index``, else auto-discover it.

    An explicit ``--index`` is guarded against path traversal — it must resolve
    inside the CWD, git root, or an exact resolver-selected target for an
    explicit ``--root`` before being trusted.

    Args:
        args: parsed top-level namespace (``args.index`` may be ``None``).
    """
    if not args.index:
        return find_index()
    resolved = Path(args.index).resolve()
    cwd = Path.cwd().resolve()
    try:
        git_root = Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=_GIT_TIMEOUT_S,
            ).strip()
        ).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        git_root = cwd
    configured_index = (
        index_paths.resolve_index(root=args.root).index_path if os.environ.get("CODEMAP_INDEX_DIR") else None
    )
    default_root_index = (
        index_paths.resolve_index(root=args.root, index_dir_override=None).index_path if args.root is not None else None
    )
    if not (
        resolved.is_relative_to(cwd)
        or resolved.is_relative_to(git_root)
        or resolved == configured_index
        or resolved == default_root_index
    ):
        # emit a parseable JSON error (not just a bare stderr + exit) so a
        # caller sees the guard rejection in the same channel as every other failure.
        _print(f"scan-query: --index path outside project root: {resolved}", file=sys.stderr)
        _die_json({"error": "index path outside project root", "path": str(resolved)}, _EXIT_BAD_INPUT)
    return resolved


def main(argv: Sequence[str] | None = None) -> None:
    """Parse CLI arguments, load the index, and dispatch to the appropriate command.

    Args:
        argv: argument vector excluding the program name. ``None`` (the
            default) falls through to argparse's own convention of reading
            ``sys.argv[1:]`` — the standalone-script path (``bin/scan-query``,
            or running this module directly). :mod:`codemap_py.cli` passes an
            explicit list when calling this in-process under its read lease.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "rdeps" and args.limit < 0:
        parser.error("rdeps --limit must be 0 or a positive integer")
    global _CMD, _force_compact_coverage, _verbose_coverage  # noqa: PLW0603
    _CMD = args.command
    _verbose_coverage = args.verbose_coverage
    _force_compact_coverage = args.compact
    _reject_multiline_args(args)

    if args.timeout > 0 and hasattr(signal, "SIGALRM"):

        def _timeout_handler(signum: int, frame: object) -> None:  # noqa: ARG001
            _print(f"scan-query: timed out after {args.timeout}s", file=sys.stderr)
            sys.exit(2)

        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(args.timeout)

    index_path = _resolve_index_path(args)
    index = _load_index_leased(index_path)
    if not _autobuild_disabled() and not args.no_heal:
        # refresh a stale index inline (bounded) so the answer reflects the
        # current tree — e.g. an edge added by a just-committed change is visible.
        index = maybe_self_heal(index, index_path, _resolve_project_root(args.root, index))
    warn_if_stale(index)
    project_root = _resolve_project_root(args.root, index)

    # flag a query resolved against a different tree than the index was built for.
    # Set before any command runs so _coverage picks it up; also warn on stderr so a
    # human sees it even if they ignore the coverage block. query_complete is forced
    # false downstream in _query_complete.
    global _root_mismatch  # noqa: PLW0603
    _root_mismatch = _detect_root_mismatch(args.root, index)
    if _root_mismatch:
        _print(
            f"⚠ codemap: index scan_root ({index.get('scan_root')}) differs from queried root "
            f"({project_root}) — result describes a different project; re-scan or pass a matching --root.",
            file=sys.stderr,
        )

    # batch and diff-impact both need the top-level parser to run their own in-process
    # sub-queries, so they route here rather than through _dispatch_command (which the
    # sub-queries themselves use). Neither may nest inside batch — see _batch_item_argv.
    if args.command == "batch":
        cmd_batch(index, args, parser, project_root)
    elif args.command == "diff-impact":
        cmd_diff_impact(index, args, parser, project_root)
    else:
        _dispatch_command(index, args, parser, project_root)


# Command → handler lookup for _dispatch_command. Each value takes the same
# (index, args, project_root) triple and extracts whatever it needs from
# args — a dict dispatch keeps _dispatch_command itself to one lookup + one
# call regardless of how many subcommands exist, instead of an ever-growing
# if/elif chain. project_root is unused by most handlers; args is passed
# through whole to the two (uncovered, dead-symbols/-modules) that take the
# full namespace rather than individual fields.
_COMMAND_HANDLERS: dict[str, Callable[[dict, argparse.Namespace, Path], None]] = {
    "deps": lambda i, a, r: cmd_deps(  # noqa: ARG005 (r unused — shared handler signature)
        i, a.module, stdlib_only=a.stdlib, third_party_only=a.third_party, internal_only=a.internal
    ),
    "rdeps": lambda i, a, r: cmd_rdeps(  # noqa: ARG005
        i, a.module, exclude_tests=a.exclude_tests, entity=_as_entity(a.entity), limit=a.limit
    ),
    "central": lambda i, a, r: cmd_central(i, a.top, exclude_tests=a.exclude_tests, entity=_as_entity(a.entity)),  # noqa: ARG005
    "coupled": lambda i, a, r: cmd_coupled(i, a.top, exclude_tests=a.exclude_tests, entity=_as_entity(a.entity)),  # noqa: ARG005
    "path": lambda i, a, r: cmd_path(i, a.frm, a.to),  # noqa: ARG005
    "list": lambda i, a, r: cmd_list(i, limit=a.limit),  # noqa: ARG005
    "packages": lambda i, a, r: cmd_packages(i),  # noqa: ARG005
    "symbol": lambda i, a, r: cmd_symbol(
        i, a.name, a.limit, exclude_tests=a.exclude_tests, with_imports=a.with_imports, project_root=r
    ),
    "symbols": lambda i, a, r: cmd_symbols(i, a.module),  # noqa: ARG005
    "find-symbol": lambda i, a, r: cmd_find_symbol(i, a.pattern, a.limit, exclude_tests=a.exclude_tests),  # noqa: ARG005
    "fn-deps": lambda i, a, r: cmd_fn_deps(i, a.qname),  # noqa: ARG005
    "fn-rdeps": lambda i, a, r: cmd_fn_rdeps(i, a.qname, exclude_tests=a.exclude_tests),  # noqa: ARG005
    "fn-central": lambda i, a, r: cmd_fn_central(i, a.top, exclude_tests=a.exclude_tests),  # noqa: ARG005
    "fn-blast": lambda i, a, r: cmd_fn_blast(i, a.qname),  # noqa: ARG005
    "test-impact": lambda i, a, r: cmd_test_impact(i, a.qname, include_mocks=a.include_mocks),  # noqa: ARG005
    "mock-rdeps": lambda i, a, r: cmd_mock_rdeps(i, a.query),  # noqa: ARG005
    "subprocess-deps": lambda i, a, r: cmd_subprocess_deps(i, a.module),  # noqa: ARG005
    "subprocess-rdeps": lambda i, a, r: cmd_subprocess_rdeps(i, a.module),  # noqa: ARG005
    "fixture-rdeps": lambda i, a, r: cmd_fixture_rdeps(i, a.fixture_name),  # noqa: ARG005
    "fixture-graph": lambda i, a, r: cmd_fixture_graph(i, a.test_file),  # noqa: ARG005
    "import-types": lambda i, a, r: cmd_import_types(i, a.module),  # noqa: ARG005
    "undocumented": lambda i, a, r: cmd_undocumented(i, a.module, all_modules=a.all_modules),  # noqa: ARG005
    "uncovered": lambda i, a, r: cmd_uncovered(i, a),  # noqa: ARG005
    "coverage": lambda i, a, r: cmd_coverage(i, a.qname),  # noqa: ARG005
    "coverage-gap": lambda i, a, r: cmd_coverage_gap(i, a.module, all_modules=a.all_modules, threshold=a.threshold),  # noqa: ARG005
    "xrefs": lambda i, a, r: cmd_xrefs(i, a.query, broken=a.broken),  # noqa: ARG005
    "dead-symbols": lambda i, a, r: cmd_dead_symbols(i, a),  # noqa: ARG005
    "dead-modules": lambda i, a, r: cmd_dead_modules(i, a),  # noqa: ARG005
}


def _dispatch_command(
    index: dict, args: argparse.Namespace, parser: argparse.ArgumentParser, project_root: Path
) -> None:
    """Route a parsed ``args`` namespace to its command handler.

    Shared by :func:`main` and :func:`cmd_batch` so a batched item runs through the
    exact same code path as a standalone invocation. ``batch`` is intentionally not
    routed here — nesting a batch inside a batch is rejected by :func:`cmd_batch`.
    Dispatch is a plain dict lookup (:data:`_COMMAND_HANDLERS`) rather than an
    if/elif chain, so adding a subcommand never grows this function's complexity.

    Args:
        index: parsed codemap index dict.
        args: the argparse namespace for a single command (``args.command`` set).
        parser: the top-level parser (unused here; kept for signature symmetry with
            :func:`cmd_batch`, which needs it to re-parse item argv).
        project_root: resolved project root for file-path lookups.
    """
    del parser  # symmetry only; _dispatch_command re-parses nothing
    handler = _COMMAND_HANDLERS.get(args.command)
    if handler is not None:
        handler(index, args, project_root)


if __name__ == "__main__":
    main()
