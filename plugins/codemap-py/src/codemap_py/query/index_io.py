"""Find, load and freshness-check the index, and build the maps every command reads."""

from __future__ import annotations

import calendar
import json
import os
import subprocess
import sys
import time
from collections import ChainMap
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from codemap_py import index_paths, rwgate
from codemap_py import query_state as state
from codemap_py.scanner import INDEXED_PATHSPEC

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
from _exclusions import Exclusions, _load_exclusions, _match_exclusion, is_excluded  # noqa: E402

from codemap_py.schema import (  # noqa: E402
    CALL_GRAPH_MIN_VER,
    MODULE_ALIASES_MIN_VER,
    VALID_CALL_RESOLUTIONS,
    validate_index,
)

from .errors import _EXIT_NOT_INDEXED, _die_json, _exit_error  # noqa: E402
from .output import _print  # noqa: E402

# v5.1: MODULE_ALIASES_MIN_VER is imported for downstream feature gating; no
# command consumes it directly today — module_aliases is applied internally by
# scan-index at import resolution time. Touch reference to avoid F401 churn.
_ = MODULE_ALIASES_MIN_VER


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


def _git_root() -> Path | None:
    """Return the git repository root, or None if not inside a git repo."""
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
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
#: Largest index file in bytes (512 MiB) the reader will load, matching what the engine serves.
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
#: Human-readable explanation for each index self-check failure slug, shown in stderr and JSON errors.
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
    if state._invocation is not None:
        state._invocation.result = {"error": code, "detail": detail}
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
    try:
        with rwgate.read_lease(index_path, **_gate_timeout_kwargs()):
            index = load_index(index_path)
        state._LOADED_INDEX_PATH = str(index_path)
        return index
    except rwgate.IndexBusy:
        _emit_gate_error("index_busy", "read lease timed out under a live writer")
    except rwgate.IndexUnreadable as exc:
        # Subclass of CoordinationUnavailable — must be caught ahead of it.
        _emit_gate_error("index_unreadable", str(exc))
    except rwgate.CoordinationUnavailable as exc:
        _emit_gate_error("index_coordination_unavailable", str(exc))
    raise AssertionError("unreachable: every gate failure above exits")  # pragma: no cover


_GIT_TIMEOUT_S = 10  # max seconds for any git subprocess (H78: hung process guard)


# The single file-set contract shared by the index writer and BOTH staleness readers.
# scan-index records ``file_shas`` for exactly these patterns, so any reader that
# narrows the set reports "fresh" for a change it simply never looked at — the v2
# timestamp fallback used to watch ``*.py`` alone, so editing a ``.pyi`` or a doc file
# left a file_shas-less index claiming freshness. Spelled ONCE here and consumed by
# :func:`_resolve_current_file_shas` and :func:`check_staleness` so the two paths
# cannot drift apart again.
#: Git pathspec of every tracked file type that can change index content, shared by the staleness checks.
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
#: Largest number of changed files for which a stale index is rebuilt inline at query time.
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
    """Return ``{path: blob_sha}`` parsed from ``git ls-files -s -z`` output.

    Drops user-excluded paths so the result matches the ``file_shas`` written by
    scan-index. Uses ``_match_exclusion`` only (NOT SKIP_DIRS): scan-index's git-blob
    ``file_shas`` path (``_git_file_hashes``) filters solely by user exclusions and
    keeps SKIP_DIR files that git tracks. Applying SKIP_DIRS here would drop those,
    making them show as "deleted" and re-introducing a false stale. Matching the
    writer exactly is the point — including its NUL-separated records: without ``-z``,
    git C-quotes a non-ASCII path (``"pkg/mod\\303\\251.py"``), which matched no key the
    writer recorded, so every such file read as deleted and the index never turned
    fresh however often it self-healed.

    Args:
        output: decoded stdout of ``git ls-files -s -z``, one
            ``<mode> <sha> <stage>\\t<path>`` record per NUL-terminated entry.

    Examples:
        >>> _parse_ls_files_stage("100644 abc 0\\tpkg/modé.py\\x00100644 def 0\\tpkg/a.py\\x00")
        {'pkg/modé.py': 'abc', 'pkg/a.py': 'def'}
    """
    exclusions = _get_exclusions_cached()
    shas: dict[str, str] = {}
    for entry in output.split("\0"):
        meta, tab, path = entry.strip().partition("\t")
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
        # UTF-8 with replacement, never the locale codec: the writer decodes the same bytes that way, so a path
        # decoded differently here would match no recorded key.
        output = subprocess.check_output(  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
            ["git", "ls-files", "-s", "-z", "--", *_INDEXED_PATHSPEC],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
            encoding="utf-8",
            errors="replace",
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
        result = subprocess.run(  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
            ["git", "log", f"--since={scanned_at}", "--name-only", "--pretty=", "--", *_INDEXED_PATHSPEC],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
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
    #
    # Stand down when a writer is already running. The prompt hook starts a detached
    # refresh on its own schedule, so a query in the same turn would otherwise spawn a
    # second scan that can only queue behind the first, then be killed at
    # _HEAL_TIMEOUT_S — paying the full timeout, healing nothing, and killing a writer
    # mid-flight. The probe is advisory; losing the race costs no more than the
    # unguarded behaviour did.
    if rwgate.writer_active(index_path):
        _print(
            "codemap: a refresh is already running — answering from the current index.",
            file=sys.stderr,
        )
        return index
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
        result = subprocess.run(  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
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


@dataclass(frozen=True)
class _SymbolTarget:
    """Outcome of resolving a caller-supplied ``fn-*`` target to one indexed ``module::symbol``.

    Attributes:
        qname: the ``module::symbol`` to query when ``found``; otherwise the caller's input unchanged.
        found: whether the target names exactly one indexed symbol (or, where accepted, a persisted alias).
        normalized_from: the caller's original spelling when it was rewritten into ``qname``, else ``None``.
        candidates: every matching ``module::symbol`` when a bare or class-qualified name is ambiguous, sorted.
    """

    qname: str
    found: bool = False
    normalized_from: str | None = None
    candidates: tuple[str, ...] = ()


def _split_at_module_prefix(module_names: set[str], known: Mapping[str, object], dotted: str) -> str | None:
    """Rewrite ``pkg.mod.func`` to ``pkg.mod::func`` at the longest module prefix naming a known symbol.

    Every split point whose left side is an indexed module is tried, longest module first, and the first rewrite that
    names a key of *known* wins. A longest-prefix split whose remainder is not a symbol falls back to a shorter
    module rather than failing, so ``pkg.mod.Class.method`` still resolves when ``pkg.mod.Class`` is not a module.

    Args:
        module_names: every indexed module name.
        known: qnames that count as a resolved target (symbol map, optionally plus alias keys).
        dotted: caller-supplied target without ``::``.

    Returns:
        The rewritten ``module::symbol``, or ``None`` when no split names a known symbol.

    Examples:
        >>> names = {"pkg", "pkg.mod"}
        >>> _split_at_module_prefix(names, {"pkg.mod::Klass.run": None}, "pkg.mod.Klass.run")
        'pkg.mod::Klass.run'
        >>> _split_at_module_prefix(names, {"pkg::helper": None}, "pkg.helper")
        'pkg::helper'
        >>> _split_at_module_prefix(names, {}, "pkg.mod.missing") is None
        True
    """
    parts = dotted.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        module = ".".join(parts[:cut])
        if module not in module_names:
            continue
        candidate = f"{module}::{'.'.join(parts[cut:])}"
        if candidate in known:
            return candidate
    return None


def _normalize_symbol_target(index: dict, sym_map: dict, target: str, *, accept_aliases: bool = False) -> _SymbolTarget:
    """Resolve a ``module::symbol``, dotted ``module.symbol``, or bare/class-qualified name to one symbol.

    The 2026-10 telemetry showed agents passing ``pkg.mod.func`` and bare ``func`` to ``fn-*`` commands and getting
    "not found", then spending extra calls on ``find-symbol``. A rewrite is accepted only when nothing else the caller
    could have meant matches; otherwise the caller gets the alternatives instead of a confident answer to a different
    question. Resolution order:

    1. Exact ``module::symbol`` hit (or a persisted alias key when *accept_aliases*) — returned unchanged. This is the
       hot path ``diff-impact`` takes once per changed symbol, so it stays a dict lookup.
    2. Any other target containing ``::``, or an exact indexed module name — not found, with no candidates. An explicit
       separator is never second-guessed, and a module name keeps the caller's module redirect or module mode.
    3. Dotted target — split at the longest indexed module prefix whose remainder is a known symbol; otherwise every
       symbol whose ``qualified_name`` (``func`` or ``Class.method``) equals the target.
    4. Exactly one such symbol, and no indexed module whose name ends in ``.<target>``, resolves. A short module name
       such as ``cli`` therefore never turns into some unrelated ``tools.cli::cli`` function.
    5. Several symbols, or any module-name suffix match, return all of them as ``candidates``.
    6. A name matching only method leaves (``open`` against ``Box.open``) returns those methods as ``candidates`` and
       never auto-resolves: nobody named the class, so even a single leaf is a guess.

    Args:
        index: parsed codemap index dict.
        sym_map: flat ``module::symbol`` lookup from :func:`build_symbol_map`.
        target: the caller-supplied symbol argument.
        accept_aliases: also accept persisted ``symbol_aliases`` keys as exact/dotted hits (``fn-rdeps`` resolves
            them itself; commands reading ``sym_map[qname]`` directly must leave this off).

    Returns:
        The resolution outcome; ``normalized_from`` is set only when the input was rewritten.

    Examples:
        >>> idx = {"modules": [{"name": "a"}, {"name": "b"}, {"name": "tools.solo"}]}
        >>> syms = {"a::run": None, "a::Box.open": None, "b::run": None, "b::solo": None, "b::only": None}
        >>> _normalize_symbol_target(idx, syms, "a::run")
        _SymbolTarget(qname='a::run', found=True, normalized_from=None, candidates=())
        >>> _normalize_symbol_target(idx, syms, "a.Box.open").qname
        'a::Box.open'
        >>> _normalize_symbol_target(idx, syms, "only").qname
        'b::only'
        >>> _normalize_symbol_target(idx, syms, "run").candidates
        ('a::run', 'b::run')
        >>> _normalize_symbol_target(idx, syms, "solo").candidates
        ('b::solo', 'tools.solo')
        >>> _normalize_symbol_target(idx, syms, "open").candidates
        ('a::Box.open',)
    """
    aliases = index.get("symbol_aliases") if accept_aliases else None
    # ChainMap answers membership across both maps without copying the symbol map: diff-impact calls this once per
    # changed symbol, and a merged dict would rebuild the whole map each time.
    known: Mapping[str, object] = ChainMap(sym_map, aliases) if isinstance(aliases, dict) and aliases else sym_map
    if target in known:
        return _SymbolTarget(target, found=True)
    module_names = {m["name"] for m in index.get("modules", []) if isinstance(m.get("name"), str)}
    if "::" in target or target in module_names:
        return _SymbolTarget(target)
    rewritten = _split_at_module_prefix(module_names, known, target) if "." in target else None
    exact = (rewritten,) if rewritten else tuple(sorted(qname for qname in sym_map if qname.endswith(f"::{target}")))
    module_suffixes = {name for name in module_names if name.endswith(f".{target}")}
    if len(exact) == 1 and not module_suffixes:
        return _SymbolTarget(exact[0], found=True, normalized_from=target)
    if exact or module_suffixes:
        return _SymbolTarget(target, candidates=tuple(sorted({*exact, *module_suffixes})))
    return _SymbolTarget(target, candidates=tuple(sorted(qname for qname in sym_map if qname.endswith(f".{target}"))))


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


_symbol_map_cache: dict | None = None


_rev_graph_cache: dict | None = None


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
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
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

    ``-z`` keeps a non-ASCII path verbatim; the default listing C-quotes it, so it could never equal an indexed path.
    """
    try:
        output = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", "*.py"],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
            **_git_cwd_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    exclusions = _get_exclusions_cached()
    return [path for path in output.split("\0") if path and not is_excluded(path, exclusions)]


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
