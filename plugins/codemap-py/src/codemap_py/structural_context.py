"""structural_context — structural context for one reviewed change set at a verified head.

``codemap-py context`` is the one call a review, resolve or remediation workflow makes to get structural evidence for
a change set. It builds or refreshes the index for the checkout under review (normally a worktree at the pull-request
head), keeps every write outside that checkout, runs ``diff-impact`` against the fresh index, and always answers with
one machine-readable record. When the index cannot serve the answer, the record names exactly one cause from the
closed :class:`ContextCause` set and carries a deterministic text-search fallback, labelled with its method and low
confidence, so a report states why structural evidence is missing instead of a generic gap.

Flow:

1. Verify the source root, read the diff, and stop as ``not-applicable`` when it names no ``.py`` path.
2. Read the root's ``HEAD`` and, when the caller passes ``--expect-head``, refuse a root at any other commit, or one
   whose indexed Python carries uncommitted or untracked changes (``head-mismatch``): an index of the wrong tree is
   the failure this command exists to prevent.
3. Build (no index yet) or refresh (``--incremental``) the index in a per-root, per-head directory under the index
   base, with the index, its coordination directory and the runtime logs all outside the source root.
4. Run ``diff-impact --no-heal`` against that exact index file and classify the answer.
5. On an index-side failure, run :func:`text_search_fallback` over the same verified root.

Building the index parses source with :func:`ast.parse` only; the scanner never imports or executes the code it
indexes, so indexing a contributor's head is reading, not running, their code.

Exit codes: ``0`` whenever a record was produced (the outcome is in ``status``), ``2`` for invalid arguments,
including an index directory inside the source root.

Examples:
    >>> ContextCause("index-stale") is ContextCause.INDEX_STALE
    True
    >>> sorted(cause.value for cause in CONSUMER_DETECTED_CAUSES)
    ['codemap-interpreter-unsupported', 'codemap-not-installed', 'context-request-invalid']
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from codemap_py import index_paths
from codemap_py.query.diff_batch import _unquote_git_path
from codemap_py.scanner.discovery import _is_python_source, _iter_python_files, path_to_module
from codemap_py.scanner.exclusions import _load_exclusions, is_excluded

__all__ = [
    "CONSUMER_DETECTED_CAUSES",
    "FALLBACK_CAUSES",
    "SCHEMA",
    "SCHEMA_VERSION",
    "ChangeSet",
    "ChangedFile",
    "ContextCause",
    "ContextRequest",
    "ContextStatus",
    "main",
    "parse_change_set",
    "resolve_context",
    "text_search_fallback",
]

#: Identifier of the record family this module writes.
SCHEMA = "codemap-py.structural-context"
#: Version of the record layout; a new family starts at 1.
SCHEMA_VERSION = 1


class ContextStatus(str, Enum):
    """Outcome of one structural-context request; the value is what the record's ``status`` field carries."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not-applicable"


class ContextCause(str, Enum):
    """Closed set of reasons structural context is unavailable or not applicable; the value is the slug.

    Slugs shared with review impact tiering keep that vocabulary (``codemap-not-installed``, ``index-stale``, ``index-
    root-mismatch``, ``query-failed``). ``codemap-*`` and ``index-*`` causes are environment problems a rerun or
    provisioning can close; the others describe the request itself.
    """

    CODEMAP_NOT_INSTALLED = "codemap-not-installed"
    CODEMAP_INTERPRETER_UNSUPPORTED = "codemap-interpreter-unsupported"
    CONTEXT_REQUEST_INVALID = "context-request-invalid"
    CODEMAP_TIMEOUT = "codemap-timeout"
    INDEX_MISSING = "index-missing"
    INDEX_STALE = "index-stale"
    INDEX_ROOT_MISMATCH = "index-root-mismatch"
    INDEX_BUSY = "index-busy"
    INDEX_BUILD_FAILED = "index-build-failed"
    QUERY_FAILED = "query-failed"
    HEAD_MISMATCH = "head-mismatch"
    SOURCE_UNAVAILABLE = "source-unavailable"
    DIFF_UNAVAILABLE = "diff-unavailable"
    NON_PYTHON_SCOPE = "non-python-scope"


#: Causes only the consumer can record, because codemap-py itself could not start or wrote no record to report them:
#: no launcher, no eligible interpreter, or a request the command refused as written (exit 2,
#: ``invalid_context_request`` on stderr), which is a consumer or version-skew defect, never a failed query.
CONSUMER_DETECTED_CAUSES = frozenset(
    {
        ContextCause.CODEMAP_NOT_INSTALLED,
        ContextCause.CODEMAP_INTERPRETER_UNSUPPORTED,
        ContextCause.CONTEXT_REQUEST_INVALID,
    }
)
#: Causes after which the root is verified and the diff readable, so the text-search fallback runs.
FALLBACK_CAUSES = frozenset(
    {
        ContextCause.CODEMAP_TIMEOUT,
        ContextCause.INDEX_MISSING,
        ContextCause.INDEX_STALE,
        ContextCause.INDEX_ROOT_MISMATCH,
        ContextCause.INDEX_BUSY,
        ContextCause.INDEX_BUILD_FAILED,
        ContextCause.QUERY_FAILED,
    }
)

#: Default per-step deadline, in seconds, for the index build and for the diff-impact query.
DEFAULT_TIMEOUT_S = 120
#: Largest accepted ``--timeout`` value, in seconds.
_MAX_TIMEOUT_S = 3600
#: Extra seconds the outer subprocess deadline allows beyond the child's own ``--timeout``.
_DEADLINE_GRACE_S = 15
#: Seconds allowed for the ``git rev-parse HEAD`` probe.
_GIT_TIMEOUT_S = 10
#: Seconds allowed for the ``git status`` clean-tree probe, which walks the whole working tree.
_STATUS_TIMEOUT_S = 60
#: Longest diagnostic tail, in characters, copied from a failed child into ``detail``.
_DETAIL_CHARS = 300
#: Accepted ``--expect-head`` spelling: an abbreviated or full hexadecimal commit id.
_HEAD_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
#: Hex characters of the root/head digest used to name the per-head index directory.
_LAYOUT_DIGEST_CHARS = 16


class ContextUsageError(ValueError):
    """Raised for a request that can never succeed as written, such as an index directory inside the source root."""


@dataclass(frozen=True)
class ContextRequest:
    """Validated inputs of one ``codemap-py context`` call.

    Attributes:
        root: Source root under review, normally a worktree checked out at the change's head.
        diff_file: Unified diff of the reviewed change set.
        index_base: Directory outside ``root`` that holds the per-head index directories.
        plugin_root: codemap-py plugin root whose ``bin/`` launchers build and query the index.
        expect_head: Commit the root must be checked out at, or ``None`` to accept its current ``HEAD``.
        build: ``False`` forbids building or refreshing; an absent or outdated index then becomes the cause.
        timeout_s: Deadline in seconds for the build and, separately, for the query.
    """

    root: Path
    diff_file: Path
    index_base: Path
    plugin_root: Path
    expect_head: str | None = None
    build: bool = True
    timeout_s: int = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class _Failure:
    """One classified reason the index could not answer."""

    cause: ContextCause
    detail: str


@dataclass(frozen=True)
class _IndexLayout:
    """Where the index for one root at one head lives; every path is outside the source root."""

    index_dir: Path
    index_path: Path
    log_dir: Path


Runner = Callable[[Sequence[str], Mapping[str, str], Path, float], "subprocess.CompletedProcess[str]"]


def _run_subprocess(
    argv: Sequence[str], env: Mapping[str, str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run one child with captured text output; raises :class:`subprocess.TimeoutExpired` past *timeout*."""
    return subprocess.run(  # noqa: S603 - argv list from plugin-owned launchers, no shell
        list(argv), env=dict(env), cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


# ---------------------------------------------------------------------------
# Change-set parsing
# ---------------------------------------------------------------------------

#: A ``def`` or ``class`` statement; group 1 is the defined name.
_DEF_LINE_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?(?:def|class)[ \t]+([A-Za-z_]\w*)")
#: Hunk header; group 1 is the enclosing-scope context git prints after the second ``@@``.
_HUNK_RE = re.compile(r"^@@ [^@]* @@ ?(.*)$")
#: Diff side that stands for "no file": the pre-image of an added file or the post-image of a deleted one.
_DEV_NULL = "/dev/null"
#: ``diff --git a/X b/Y`` header used only when a file section carries no ``---``/``+++`` pair.
_GIT_HEADER_RE = re.compile(r"^diff --git (\"?a/.+?\"?) (\"?b/.+\"?)$")


@dataclass(frozen=True)
class ChangedFile:
    """One Python file the reviewed change set touches.

    Attributes:
        path: Repository-relative POSIX path; the pre-image path when the change deletes or renames the file away.
        deleted: ``True`` when the module no longer exists at the head, so its importers are the impact.
        symbols: Sorted ``def``/``class`` names the hunks add, remove or sit inside; dunder names are dropped.
    """

    path: str
    deleted: bool
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangeSet:
    """Python files of one unified diff, in path order."""

    files: tuple[ChangedFile, ...]

    @property
    def python_paths(self) -> tuple[str, ...]:
        """Return every changed ``.py`` path, sorted."""
        return tuple(sorted({changed.path for changed in self.files}))


@dataclass
class _FileSection:
    """Mutable accumulator for one ``diff --git`` section while the diff is read."""

    header: str = ""
    old: str | None = None
    new: str | None = None
    saw_pair: bool = False
    rename_from: str | None = None
    symbols: set[str] = field(default_factory=set)


def _strip_side(token: str, prefix: str) -> str | None:
    """Return the repository path of one diff side, or ``None`` for ``/dev/null``.

    Examples:
        >>> _strip_side("a/pkg/mod.py", "a/"), _strip_side("/dev/null", "a/")
        ('pkg/mod.py', None)
    """
    path = _unquote_git_path(token.split("\t", 1)[0].strip())
    if path == _DEV_NULL:
        return None
    return path[len(prefix) :] if path.startswith(prefix) else path


def _add_symbol(section: _FileSection, text: str) -> None:
    """Record the name a ``def``/``class`` line defines, skipping dunder names."""
    match = _DEF_LINE_RE.match(text)
    if match and not (match.group(1).startswith("__") and match.group(1).endswith("__")):
        section.symbols.add(match.group(1))


def _section_files(section: _FileSection) -> list[ChangedFile]:
    """Turn one finished diff section into its changed Python files (zero, one, or two for a rename)."""
    old, new = section.old, section.new
    if not section.saw_pair:
        header = _GIT_HEADER_RE.match(section.header)
        if header:
            old, new = _strip_side(header.group(1), "a/"), _strip_side(header.group(2), "b/")
    if section.rename_from is not None:
        old = section.rename_from
    files: list[ChangedFile] = []
    symbols = tuple(sorted(section.symbols))
    if new is not None and new.endswith(".py"):
        files.append(ChangedFile(path=new, deleted=False, symbols=symbols))
    if old is not None and old.endswith(".py") and old != new:
        files.append(ChangedFile(path=old, deleted=True, symbols=symbols if new is None else ()))
    return files


def _read_header_line(section: _FileSection, line: str) -> None:
    """Apply one file-header line (``---``, ``+++``, ``rename from``) to *section*."""
    if line.startswith("--- "):
        section.old, section.saw_pair = _strip_side(line[4:], "a/"), True
    elif line.startswith("+++ "):
        section.new, section.saw_pair = _strip_side(line[4:], "b/"), True
    elif line.startswith("rename from "):
        section.rename_from = _unquote_git_path(line[len("rename from ") :].strip())


def parse_change_set(diff_text: str) -> ChangeSet:
    """Read the changed Python files and their touched symbols from a unified diff.

    Deleted modules and the old side of a rename are kept with ``deleted=True``: their importers are exactly what a
    reviewer must check. Header lines inside a hunk are content, so ``--- x`` in a removed line is never a new path.

    Args:
        diff_text: Unified diff text, as ``git diff`` or a pull-request ``.diff`` writes it.

    Returns:
        The :class:`ChangeSet` of ``.py`` files, sorted by path then deletion flag.

    Examples:
        >>> diff = (
        ...     "diff --git a/pkg/core.py b/pkg/core.py\\n--- a/pkg/core.py\\n+++ b/pkg/core.py\\n"
        ...     "@@ -1,2 +1,2 @@ def helper():\\n-    return 1\\n+    return 2\\n"
        ... )
        >>> parse_change_set(diff).files
        (ChangedFile(path='pkg/core.py', deleted=False, symbols=('helper',)),)
        >>> parse_change_set("diff --git a/README.md b/README.md\\n").python_paths
        ()
    """
    sections: list[_FileSection] = []
    section: _FileSection | None = None
    in_hunk = False
    scope = ""
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            section, in_hunk = _FileSection(header=line), False
            sections.append(section)
            continue
        if section is None:
            section = _FileSection()
            sections.append(section)
        hunk = _HUNK_RE.match(line)
        if hunk:
            in_hunk, scope = True, hunk.group(1)
        elif not in_hunk:
            _read_header_line(section, line)
        elif line[:1] == " " and _DEF_LINE_RE.match(line[1:]):
            scope = line[1:]
        elif line[:1] in "+-":
            # Nearest def/class above the change, from the hunk header or an unchanged line, is its enclosing scope.
            _add_symbol(section, scope)
            _add_symbol(section, line[1:])
    files = {(changed.path, changed.deleted): changed for each in sections for changed in _section_files(each)}
    return ChangeSet(files=tuple(files[key] for key in sorted(files)))


# ---------------------------------------------------------------------------
# Text-search fallback
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FallbackLimits:
    """Deterministic bounds of one text search; every cap that bites sets a ``truncated`` flag in the output."""

    max_files: int = 50_000
    max_file_bytes: int = 1_048_576
    max_targets: int = 40
    max_listed: int = 50


#: What a line-oriented text search cannot see; copied verbatim into every fallback record.
_FALLBACK_NOT_COVERED = (
    "dynamic imports and attribute dispatch (importlib, __import__, getattr, registries)",
    "string references (entry points, configuration files, plugin registries)",
    "member names on continuation lines of a parenthesized from-import",
    "re-exports through an intermediate module",
    "aliased names (import x as y, from m import f as g) and calls that never spell the changed name",
)
#: Fixed explanation attached to every fallback record.
_FALLBACK_NOTE = (
    "Text search over the verified source root. Hits are candidate importers and references, not resolved edges. "
    "Zero hits means unknown, never no impact: this evidence may raise review depth but never lower it."
)
#: ``import a.b, c as d`` statement; group 1 holds the module list.
_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+([\w., \t]+)")
#: ``from .a.b import x, y`` statement; groups: leading dots, module, member list.
_FROM_RE = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+(.*)$")


def _module_name(root: Path, rel: str, src_roots: tuple[Path, ...]) -> str:
    """Return the dotted module name of *rel*, relative to the first source root that contains it.

    Examples:
        >>> _module_name(Path("/r"), "src/pkg/__init__.py", (Path("/r/src"), Path("/r")))
        'pkg'
        >>> _module_name(Path("/r"), "tests/test_a.py", (Path("/r/src"), Path("/r")))
        'tests.test_a'
    """
    filepath = root / rel
    for candidate in src_roots:
        if candidate == root or candidate in filepath.parents:
            return path_to_module(filepath, candidate)
    return path_to_module(filepath, root)


def _src_roots(root: Path) -> tuple[Path, ...]:
    """Return the source roots used to name modules, most specific first, ending with *root* itself."""
    from codemap_py.graph import _resolve_src_roots  # deferred: graph is heavy and only the fallback needs it

    context = _resolve_src_roots(root)
    ordered = [*context.configured, context.default, root]
    return tuple(dict.fromkeys(ordered))


def _members(text: str) -> list[str]:
    """Return the imported names of a ``from`` statement's member list, without aliases, parentheses or comments.

    Examples:
        >>> _members("(a, b as c,  # note")
        ['a', 'b']
    """
    names = []
    for part in text.split("#", 1)[0].replace("(", " ").replace(")", " ").replace("\\", " ").split(","):
        name = part.strip().split(" as ", 1)[0].strip()
        if name.isidentifier():
            names.append(name)
    return names


def _relative_base(importer: str, is_package: bool, level: int) -> list[str] | None:
    """Return the package parts a relative import of *level* dots resolves against, or ``None`` past the top.

    Examples:
        >>> _relative_base("pkg.sub.mod", False, 1), _relative_base("pkg.sub", True, 2)
        (['pkg', 'sub'], ['pkg'])
        >>> _relative_base("mod", False, 2) is None
        True
    """
    parts = importer.split(".") if importer else []
    package = parts if is_package else parts[:-1]
    if level - 1 > len(package):
        return None
    return package[: len(package) - (level - 1)]


def imported_modules(line: str, importer: str, is_package: bool) -> set[str]:
    """Return every absolute dotted name one import line can bind, with relative imports resolved.

    ``from a import b`` yields ``a`` and ``a.b``, because ``b`` may be a submodule.

    Examples:
        >>> sorted(imported_modules("from pkg.core import helper", "pkg.user", False))
        ['pkg.core', 'pkg.core.helper']
        >>> sorted(imported_modules("from . import core", "pkg.user", False))
        ['pkg', 'pkg.core']
        >>> sorted(imported_modules("import pkg.core as c, os", "x", False))
        ['os', 'pkg.core']
    """
    match = _IMPORT_RE.match(line)
    if match:
        return {part.strip().split(" as ", 1)[0].strip() for part in match.group(1).split(",") if part.strip()}
    match = _FROM_RE.match(line)
    if not match:
        return set()
    dots, module, members = match.groups()
    if dots:
        base = _relative_base(importer, is_package, len(dots))
        if base is None:
            return set()
        absolute = ".".join([*base, *([module] if module else [])])
    else:
        absolute = module
    if not absolute:
        return set()
    return {absolute, *(f"{absolute}.{name}" for name in _members(members))}


def _alternation(words: Sequence[str], *, after_dot: bool) -> re.Pattern[str] | None:
    """Compile one whole-word alternation over escaped *words*, longest first; ``None`` when empty.

    Module names must not follow a dot (``x.pkg.core`` is not ``pkg.core``); symbol names may (``self.helper()``).

    Examples:
        >>> [m.group(1) for m in _alternation(["pkg", "pkg.core"], after_dot=False).finditer("pkg.core.f(x.pkg)")]
        ['pkg.core']
        >>> [m.group(1) for m in _alternation(["helper"], after_dot=True).finditer("self.helper(helpers)")]
        ['helper']
    """
    if not words:
        return None
    body = "|".join(re.escape(word) for word in sorted(words, key=lambda word: (-len(word), word)))
    guard = r"(?<!\w)" if after_dot else r"(?<![\w.])"
    return re.compile(rf"{guard}({body})(?!\w)")


@dataclass
class _SearchState:
    """Accumulated hits of one text search: importer files per module, reference files per (path, symbol)."""

    importers: dict[str, set[str]]
    references: dict[tuple[str, str], set[str]]
    files_scanned: int = 0
    files_skipped_large: int = 0


def _python_sources(root: Path) -> list[str]:
    """Return the root's indexable ``.py``/``.pyi`` files as sorted POSIX paths, under the scanner's own exclusions."""
    files, _counts = _iter_python_files(root, _load_exclusions(root))
    return sorted(path.relative_to(root).as_posix() for path in files)


def _read_capped(path: Path, limit: int) -> str | None:
    """Return the text of *path*, or ``None`` when it is larger than *limit* bytes or unreadable."""
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _importer_hits(line: str, rel: str, targets: _SearchTargets) -> set[str]:
    """Return the target modules one line imports, or uses by dotted attribute path, from a file other than theirs."""
    if line.lstrip().startswith(("import ", "from ")):
        importer = targets.module_of[rel]
        names = imported_modules(line, importer, rel.endswith("__init__.py"))
        hits = {module for name in names for module in targets.modules_for(name)}
    elif targets.module_re is not None:
        hits = {m.group(1) for m in targets.module_re.finditer(line) if line[m.end() : m.end() + 1] == "."}
    else:
        hits = set()
    return {module for module in hits if targets.module_paths[module] != rel}


def _scan_file(text: str, rel: str, targets: _SearchTargets, state: _SearchState) -> None:
    """Record every import of, attribute use of, and whole-word reference to a target found in one file."""
    for line in text.splitlines():
        for module in _importer_hits(line, rel, targets):
            state.importers[module].add(rel)
        if targets.symbol_re is None:
            continue
        for match in targets.symbol_re.finditer(line):
            for owner in targets.symbol_paths[match.group(1)]:
                if owner != rel:
                    state.references[(owner, match.group(1))].add(rel)


@dataclass
class _SearchTargets:
    """What one text search looks for: changed modules and changed symbol names, with compiled patterns."""

    module_paths: dict[str, str]
    symbol_paths: dict[str, list[str]]
    module_of: dict[str, str]
    module_re: re.Pattern[str] | None = None
    symbol_re: re.Pattern[str] | None = None

    def modules_for(self, name: str) -> list[str]:
        """Return the target modules *name* is, or is a submodule or member of."""
        return [module for module in self.module_paths if name == module or name.startswith(module + ".")]


def _listing(hits: set[str], limit: int) -> dict[str, object]:
    """Return a sorted, capped file listing with its full count and an explicit truncation flag."""
    ordered = sorted(hits)
    return {"files": ordered[:limit], "file_count": len(ordered), "truncated": len(ordered) > limit}


def text_search_fallback(root: Path, change: ChangeSet, limits: FallbackLimits | None = None) -> dict[str, object]:
    """Find candidate importers of changed modules and references to changed symbols by text search.

    Used only when the index cannot answer. The search is line-oriented, uses :func:`re.escape` for every name taken
    from the diff, walks files in sorted order under the scanner's exclusions, and never executes or imports the
    searched code. Output is identical for identical inputs. ``query_complete`` is always ``false``.

    Args:
        root: Verified source root at the reviewed head.
        change: Parsed change set whose files and symbols are the search targets.
        limits: Optional bounds; defaults to :class:`FallbackLimits`.

    Returns:
        The fallback record: method, confidence, per-module importer files, per-symbol reference files, bounds that
        applied, and what a text search cannot see.
    """
    limits = limits or FallbackLimits()
    sources = _python_sources(root)
    scan_truncated = len(sources) > limits.max_files
    sources = sources[: limits.max_files]
    src_roots = _src_roots(root)
    module_of = {rel: _module_name(root, rel, src_roots) for rel in sources}
    changed = list(change.files)
    module_paths = {_module_name(root, item.path, src_roots): item.path for item in changed}
    modules = sorted(module_paths)[: limits.max_targets]
    symbol_paths: dict[str, list[str]] = {}
    for item in changed:
        for name in item.symbols:
            symbol_paths.setdefault(name, []).append(item.path)
    symbols = sorted(symbol_paths)[: limits.max_targets]
    targets = _SearchTargets(
        module_paths={module: module_paths[module] for module in modules},
        symbol_paths={name: sorted(set(symbol_paths[name])) for name in symbols},
        module_of=module_of,
        module_re=_alternation(modules, after_dot=False),
        symbol_re=_alternation(symbols, after_dot=True),
    )
    state = _SearchState(
        importers={module: set() for module in modules},
        references={(path, name): set() for name in symbols for path in targets.symbol_paths[name]},
    )
    for rel in sources:
        text = _read_capped(root / rel, limits.max_file_bytes)
        if text is None:
            state.files_skipped_large += 1
            continue
        state.files_scanned += 1
        _scan_file(text, rel, targets, state)
    deleted = {item.path for item in changed if item.deleted}
    return {
        "method": "text-search",
        "confidence": "low",
        "query_complete": False,
        "files_scanned": state.files_scanned,
        "files_skipped": state.files_skipped_large,
        "scan_truncated": scan_truncated,
        "modules": [
            {"module": module, "path": targets.module_paths[module], "deleted": targets.module_paths[module] in deleted}
            | {f"importer_{key}": value for key, value in _listing(state.importers[module], limits.max_listed).items()}
            for module in modules
        ],
        "modules_truncated": len(module_paths) > limits.max_targets,
        "symbols": [
            {"name": name, "path": path}
            | {f"reference_{key}": value for key, value in _listing(hits, limits.max_listed).items()}
            for (path, name), hits in sorted(state.references.items())
        ],
        "symbols_truncated": len(symbol_paths) > limits.max_targets,
        "not_covered": list(_FALLBACK_NOT_COVERED),
        "note": _FALLBACK_NOTE,
    }


# ---------------------------------------------------------------------------
# Index build and query
# ---------------------------------------------------------------------------


def _git_head(root: Path) -> str | None:
    """Return the full commit id checked out at *root*, or ``None`` when it is not a readable Git checkout."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],  # noqa: S607 - git resolved via PATH on purpose
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head = proc.stdout.strip()
    return head if proc.returncode == 0 and _HEAD_RE.match(head) else None


def head_matches(head: str | None, expected: str) -> bool:
    """Return whether the checked-out *head* is the *expected* commit, accepting an abbreviated expected id.

    Examples:
        >>> head_matches("f6e343a552dc6b2e6084b0cbd7588981064c2a56", "F6E343A5")
        True
        >>> head_matches(None, "f6e343a5"), head_matches("abc1234def", "abc1235")
        (False, False)
    """
    return head is not None and head.lower().startswith(expected.lower())


def index_layout(request: ContextRequest, head: str | None) -> _IndexLayout:
    """Return the per-root, per-head index directory under the request's index base.

    Keying by root and head keeps concurrent reviews of different heads apart, and lets a later run at the same head
    refresh the same index instead of building another.
    """
    mode = "commit" if request.expect_head else "working-tree"
    key = f"{index_paths.root_key(request.root)}\0{head or 'no-git'}\0{mode}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:_LAYOUT_DIGEST_CHARS]
    index_dir = request.index_base / digest
    identity = index_paths.resolve_index(root=request.root, index_dir_override=str(index_dir))
    return _IndexLayout(index_dir=index_dir, index_path=identity.index_path, log_dir=index_dir / "logs")


def _child_env(layout: _IndexLayout) -> dict[str, str]:
    """Return the child environment that pins the index, its coordination directory and logs to *layout*."""
    env = dict(os.environ)
    env["CODEMAP_INDEX_DIR"] = str(layout.index_dir)
    env["CODEMAP_LOG_DIR"] = str(layout.log_dir)
    # This command decides when to build; the query below runs with --no-heal and never builds.
    env.pop("SCAN_NO_AUTOBUILD", None)
    return env


def _tail(text: str) -> str:
    """Return the last non-empty line of *text*, bounded to :data:`_DETAIL_CHARS` characters."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][-_DETAIL_CHARS:] if lines else ""


def _classify_child_failure(proc: subprocess.CompletedProcess[str], step: str) -> _Failure:
    """Map a failed build or query child to its cause from what it printed, never from its exit code alone.

    A scan that hits its own ``--timeout`` exits ``2``, the same code as a usage error, so the printed diagnostic
    decides between ``codemap-timeout``, ``index-busy`` and the step's own failure cause.
    """
    text = f"{proc.stdout}\n{proc.stderr}"
    detail = f"{step} exited {proc.returncode}: {_tail(proc.stderr) or _tail(proc.stdout) or 'no diagnostic'}"
    if "index_busy" in text:
        return _Failure(ContextCause.INDEX_BUSY, detail)
    if "timed out" in text or '"error": "timeout"' in text or '"error":"timeout"' in text:
        return _Failure(ContextCause.CODEMAP_TIMEOUT, detail)
    cause = ContextCause.INDEX_BUILD_FAILED if step == "index build" else ContextCause.QUERY_FAILED
    return _Failure(cause, detail)


def _stored_git_sha(index_path: Path) -> str | None:
    """Return the commit id an index records it was built at, or ``None`` when unreadable or absent."""
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("git_sha") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def _head_failure(index_path: Path, head: str | None) -> _Failure | None:
    """Return ``index-stale`` when the index records a different commit than the source root's head."""
    stored = _stored_git_sha(index_path)
    if head is None or stored is None or stored == head:
        return None
    return _Failure(ContextCause.INDEX_STALE, f"index built at {stored[:12]}, source root at {head[:12]}")


def _ensure_index(
    request: ContextRequest, layout: _IndexLayout, head: str | None, runner: Runner
) -> tuple[str, _Failure | None]:
    """Build, refresh or reuse the index; return the action taken and the failure, if any."""
    exists = layout.index_path.is_file()
    if not request.build:
        if not exists:
            return "none", _Failure(ContextCause.INDEX_MISSING, "no index at this head and building is disabled")
        return "reused", _head_failure(layout.index_path, head)
    action = "refreshed" if exists else "built"
    failure = _run_build(request, layout, runner, incremental=exists)
    if failure is None and not layout.index_path.is_file():
        failure = _Failure(ContextCause.INDEX_BUILD_FAILED, "index build reported success but wrote no index")
    return action, failure or _head_failure(layout.index_path, head)


def _run_build(request: ContextRequest, layout: _IndexLayout, runner: Runner, *, incremental: bool) -> _Failure | None:
    """Run ``scan-index`` for the root into *layout* under the request's deadline; return the failure, if any."""
    argv = [sys.executable, str(request.plugin_root / "bin" / "scan-index"), "--root", str(request.root)]
    argv += ["--timeout", str(request.timeout_s), *(["--incremental"] if incremental else [])]
    try:
        proc = runner(argv, _child_env(layout), request.root, request.timeout_s + _DEADLINE_GRACE_S)
    except subprocess.TimeoutExpired:
        return _Failure(ContextCause.CODEMAP_TIMEOUT, f"index build exceeded {request.timeout_s}s")
    except OSError as exc:
        return _Failure(ContextCause.INDEX_BUILD_FAILED, f"index build could not start: {exc}")
    return _classify_child_failure(proc, "index build") if proc.returncode != 0 else None


def _query_diff_impact(
    request: ContextRequest, layout: _IndexLayout, runner: Runner
) -> tuple[dict | None, _Failure | None]:
    """Run ``diff-impact --no-heal`` against the exact index file; return the answer or the classified failure."""
    argv = [sys.executable, str(request.plugin_root / "bin" / "scan-query"), "--index", str(layout.index_path)]
    argv += ["--root", str(request.root), "--no-heal", "--timeout", str(request.timeout_s)]
    argv += ["diff-impact", "--diff-file", str(request.diff_file)]
    try:
        proc = runner(argv, _child_env(layout), request.root, request.timeout_s + _DEADLINE_GRACE_S)
    except subprocess.TimeoutExpired:
        return None, _Failure(ContextCause.CODEMAP_TIMEOUT, f"diff-impact query exceeded {request.timeout_s}s")
    except OSError as exc:
        return None, _Failure(ContextCause.QUERY_FAILED, f"diff-impact query could not start: {exc}")
    if proc.returncode != 0:
        return None, _classify_child_failure(proc, "diff-impact query")
    try:
        answer = json.loads(proc.stdout)
    except ValueError:
        return None, _Failure(ContextCause.QUERY_FAILED, "diff-impact query printed no JSON answer")
    return _classify_answer(answer)


def _classify_answer(answer: object) -> tuple[dict | None, _Failure | None]:
    """Return a parsed diff-impact answer, or the cause it cannot be trusted: an error, a stale or foreign index.

    Examples:
        >>> _classify_answer({"index": {"stale": True}})[1].cause.value
        'index-stale'
        >>> _classify_answer({"error": "index_busy"})[1].detail
        'diff-impact query error: index_busy'
        >>> _classify_answer({"index": {"stale": False, "root_mismatch": False}})[1] is None
        True
    """
    if not isinstance(answer, dict) or "error" in answer:
        code = answer.get("error") if isinstance(answer, dict) else "non-object answer"
        return None, _Failure(ContextCause.QUERY_FAILED, f"diff-impact query error: {code}")
    meta = answer.get("index") if isinstance(answer.get("index"), dict) else {}
    if meta.get("stale") is True:
        return answer, _Failure(ContextCause.INDEX_STALE, "diff-impact reports the index older than the source root")
    if meta.get("root_mismatch") is True:
        return answer, _Failure(ContextCause.INDEX_ROOT_MISMATCH, "diff-impact reports an index of another root")
    return answer, None


def _degraded_reasons(answer: dict, change: ChangeSet) -> list[str]:
    """Return why an index answer is incomplete; empty when it is complete and covers every changed module.

    ``diff-impact`` reads post-image paths only and still calls its answer complete, so a deleted or renamed-away
    module, whose importers are the real impact, is named here from the parsed diff.
    """
    meta = answer.get("index") if isinstance(answer.get("index"), dict) else {}
    reasons: list[str] = []
    if meta.get("query_complete") is not True:
        reasons.append(f"completeness: {meta.get('completeness_reason') or 'not reported'}")
    unmapped = answer.get("unmapped_files")
    if isinstance(unmapped, list) and unmapped:
        reasons.append(f"{len(unmapped)} changed Python file(s) not in the index")
    deleted = sorted(changed.path for changed in change.files if changed.deleted)
    if deleted:
        named = ", ".join(deleted[:5])
        reasons.append(
            f"{len(deleted)} deleted or renamed-away Python module(s) not analysed (importers unchecked): {named}"
        )
    return reasons


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _record(request: ContextRequest) -> dict[str, object]:
    """Return a record with every field present, before any step has run."""
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "status": ContextStatus.UNAVAILABLE.value,
        "cause": None,
        "detail": "",
        "root": str(request.root),
        "diff_file": str(request.diff_file),
        "expected_head": request.expect_head,
        "head": None,
        "changed_python_files": [],
        "index": None,
        "method": None,
        "confidence": None,
        "query_complete": False,
        "degraded_reasons": [],
        "diff_impact": None,
        "fallback": None,
        "fallback_error": None,
    }


def _unavailable(record: dict[str, object], failure: _Failure) -> dict[str, object]:
    """Mark *record* unavailable with *failure*'s cause and detail."""
    record.update(status=ContextStatus.UNAVAILABLE.value, cause=failure.cause.value, detail=failure.detail)
    return record


def _with_fallback(record: dict[str, object], request: ContextRequest, change: ChangeSet) -> dict[str, object]:
    """Attach the text-search fallback and its method and confidence to an unavailable record.

    A tree that broke the build can break the walk too; the record then keeps its cause and says why no fallback came.
    """
    try:
        fallback = text_search_fallback(request.root, change)
    except (OSError, ValueError, RecursionError) as exc:
        record["fallback_error"] = f"text search failed: {type(exc).__name__}: {exc}"[:_DETAIL_CHARS]
        return record
    record.update(fallback=fallback, method=fallback["method"], confidence=fallback["confidence"])
    return record


def _status_paths(output: bytes) -> list[str]:
    """Return every path named by ``git status --porcelain=v1 -z`` output, both sides of a rename or copy included.

    Example:
        >>> _status_paths(b" M pkg/a.py\\0R  pkg/new.py\\0pkg/old.py\\0?? notes.md\\0")
        ['pkg/a.py', 'pkg/new.py', 'pkg/old.py', 'notes.md']
    """
    entries = output.decode("utf-8", "surrogateescape").split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry, index = entries[index], index + 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" and index < len(entries):
            paths.append(entries[index])
            index += 1
    return paths


def _uncommitted_python(root: Path) -> list[str] | None:
    """Return the indexed Python paths whose working-tree state differs from the checked-out commit.

    Modified, staged, deleted, renamed and untracked files count, but only those the scanner would index: a review
    artifact under a dot-directory or a non-Python note never reaches the index. ``None`` when ``git status`` cannot
    answer, so the caller cannot claim the tree is the commit's.
    """
    # Git status can otherwise rewrite the index to cache refreshed stat data, even on a clean tree.
    argv = [
        "git",
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored",
        "--",
        "*.py",
        "*.pyi",
    ]
    try:
        proc = subprocess.run(argv, cwd=root, capture_output=True, timeout=_STATUS_TIMEOUT_S, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not isinstance(proc.stdout, bytes):
        return None
    exclusions = _load_exclusions(root)
    paths = {path for path in _status_paths(proc.stdout) if _is_python_source(path.rsplit("/", 1)[-1])}
    return sorted(path for path in paths if not is_excluded(path, exclusions))


def _head_problem(request: ContextRequest, head: str | None) -> _Failure | None:
    """Return ``head-mismatch`` unless the root's tree is exactly the expected commit's tree, or ``None``.

    Without ``--expect-head`` the record describes the working tree as found and nothing is refused. With it, the root
    must be checked out at that commit and its indexed Python must carry no uncommitted or untracked change, since the
    scanner reads the working tree and the record credits its answer to the commit.
    """
    if not request.expect_head:
        return None
    if not head_matches(head, request.expect_head):
        found = head[:12] if head else "no readable Git HEAD"
        return _Failure(ContextCause.HEAD_MISMATCH, f"source root is at {found}, not {request.expect_head}")
    dirty = _uncommitted_python(request.root)
    if dirty is None:
        return _Failure(
            ContextCause.HEAD_MISMATCH, "git status failed: the root's tree cannot be verified as the head's"
        )
    if dirty:
        named = ", ".join(dirty[:3]) + (", ..." if len(dirty) > 3 else "")
        detail = f"source root at {str(head)[:12]} has {len(dirty)} uncommitted or untracked Python path(s): {named}"
        return _Failure(ContextCause.HEAD_MISMATCH, detail[:_DETAIL_CHARS])
    return None


def _preflight(request: ContextRequest, record: dict[str, object]) -> tuple[ChangeSet | None, _Failure | None]:
    """Verify the root, the diff and the head before any index work; return the change set or the stop reason."""
    if not request.root.is_dir():
        return None, _Failure(ContextCause.SOURCE_UNAVAILABLE, "source root is not a readable directory")
    try:
        change = parse_change_set(request.diff_file.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return None, _Failure(ContextCause.DIFF_UNAVAILABLE, f"diff file unreadable: {type(exc).__name__}")
    record["changed_python_files"] = list(change.python_paths)
    if not change.python_paths:
        return change, _Failure(ContextCause.NON_PYTHON_SCOPE, "the diff changes no .py file")
    head = _git_head(request.root)
    record["head"] = head
    return change, _head_problem(request, head)


def resolve_context(request: ContextRequest, runner: Runner = _run_subprocess) -> dict[str, object]:
    """Produce the structural-context record for one change set at one verified head.

    Args:
        request: Validated request.
        runner: Child-process runner; tests substitute it to reach timeout and failure paths deterministically.

    Returns:
        The JSON-ready record. ``status`` is ``available``, ``degraded``, ``unavailable`` or ``not-applicable``;
        ``cause`` is a :class:`ContextCause` slug exactly when the status is ``unavailable`` or ``not-applicable``.
    """
    record = _record(request)
    change, stop = _preflight(request, record)
    if stop is not None:
        if stop.cause is ContextCause.NON_PYTHON_SCOPE:
            record.update(status=ContextStatus.NOT_APPLICABLE.value, cause=stop.cause.value, detail=stop.detail)
            return record
        return _unavailable(record, stop)
    head = record["head"] if isinstance(record["head"], str) else None
    layout = index_layout(request, head)
    action, failure = _ensure_index(request, layout, head, runner)
    record["index"] = {"path": str(layout.index_path), "dir": str(layout.index_dir), "action": action}
    answer = None
    if failure is None:
        answer, failure = _query_diff_impact(request, layout, runner)
    if failure is not None:
        return _with_fallback(_unavailable(record, failure), request, change)
    reasons = _degraded_reasons(answer, change)
    meta = answer.get("index") if isinstance(answer.get("index"), dict) else {}
    record.update(
        status=(ContextStatus.DEGRADED if reasons else ContextStatus.AVAILABLE).value,
        method=meta.get("method") or "static-ast",
        confidence="exact",
        query_complete=meta.get("query_complete") is True,
        degraded_reasons=reasons,
        diff_impact=answer,
    )
    return record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def default_index_base() -> Path:
    """Return the default index base: a ``codemap-context`` directory in the host's temporary directory."""
    base = os.environ.get("TMPDIR") or (tempfile.gettempdir() if os.name == "nt" else "/tmp")  # noqa: S108
    return Path(base) / "codemap-context"


def _parser() -> argparse.ArgumentParser:
    """Build the ``codemap-py context`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="codemap-py context",
        description="Build or refresh the index for a checkout at a verified head, outside that checkout, and report "
        "diff-impact or the closed cause it is unavailable plus a text-search fallback.",
    )
    parser.add_argument("--root", required=True, type=Path, help="Source root under review (worktree at the head).")
    parser.add_argument("--diff-file", required=True, type=Path, help="Unified diff of the reviewed change set.")
    parser.add_argument("--index-dir", type=Path, default=None, help="Index base outside the root (default: temp dir).")
    parser.add_argument("--expect-head", default=None, help="Commit the root must be checked out at.")
    parser.add_argument("--no-build", action="store_true", help="Never build or refresh; report the missing index.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="Seconds per build and per query.")
    parser.add_argument("--out", type=Path, default=None, help="Also write the record to this file.")
    return parser


def build_request(args: argparse.Namespace, plugin_root: Path) -> ContextRequest:
    """Validate parsed arguments into a :class:`ContextRequest`.

    Raises:
        ContextUsageError: For an invalid head spelling, an out-of-range timeout, or an index base inside the root.
    """
    if args.expect_head is not None and not _HEAD_RE.match(args.expect_head):
        raise ContextUsageError("--expect-head must be a 7 to 64 character hexadecimal commit id")
    if not 1 <= args.timeout <= _MAX_TIMEOUT_S:
        raise ContextUsageError(f"--timeout must be between 1 and {_MAX_TIMEOUT_S} seconds")
    root = Path(os.path.realpath(args.root.expanduser()))
    index_base = Path(os.path.realpath((args.index_dir or default_index_base()).expanduser()))
    if index_paths.is_within(index_base, root):
        raise ContextUsageError("--index-dir must be outside the source root, so the reviewed tree stays unchanged")
    return ContextRequest(
        root=root,
        diff_file=Path(os.path.realpath(args.diff_file.expanduser())),
        index_base=index_base,
        plugin_root=plugin_root,
        expect_head=args.expect_head,
        build=not args.no_build and os.environ.get("SCAN_NO_AUTOBUILD") != "1",
        timeout_s=args.timeout,
    )


def _write_out(path: Path, text: str) -> None:
    """Write *text* to *path* through a sibling temporary file, so a reader never sees a partial record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def main(argv: Sequence[str], plugin_root: Path) -> int:
    """Run ``codemap-py context``: print the record as JSON and optionally write it to ``--out``.

    Returns:
        ``0`` when a record was produced, whatever its status; ``2`` for invalid arguments, with one JSON error line
        on stderr.
    """
    try:
        args = _parser().parse_args(list(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        request = build_request(args, plugin_root)
    except ContextUsageError as exc:
        sys.stderr.write(json.dumps({"error": "invalid_context_request", "detail": str(exc)}) + "\n")
        return 2
    text = json.dumps(resolve_context(request), indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        try:
            _write_out(args.out.expanduser(), text)
        except OSError as exc:
            sys.stderr.write(json.dumps({"error": "context_out_unwritable", "detail": type(exc).__name__}) + "\n")
    sys.stdout.write(text)
    return 0
