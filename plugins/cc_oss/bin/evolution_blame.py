"""SZZ blame evidence for ``/oss:analyse evolution``: which earlier commits wrote the lines each fix repaired.

For every commit of a fix entry the script diffs the commit against its first parent (merge commits included),
blames the pre-fix lines the commit modified or deleted with ``git blame -w -M -C`` at that parent, and counts the
blamed lines per originating commit SHA. Add-only hunks (a missing guard) have no pre-fix lines of their own, so up to
three context lines on each side of the insertion point are blamed instead and kept apart as ``context_blamed``.
Tests, docs, changelog and news files, lock files and generated files are excluded by default
(:data:`DEFAULT_EXCLUDES`).

Usage::

    python evolution_blame.py evidence --repo DIR --entries FILE --out FILE [--budget-s N] [--timeout N]
        [--exclude GLOB ...]

``--entries`` holds the changelog module's fix entries: either the ``fixes`` list itself or the whole
``build_entries`` result. ``--exclude`` (repeatable) replaces :data:`DEFAULT_EXCLUDES`. ``--budget-s`` is the
wall-clock budget per fix (unlimited when omitted); ``--timeout`` caps every single git call (default 60 s).

Outputs:
    ``--out`` is rewritten as JSONL, one evidence record per fix with the keys of ``RECORD_FIELDS["evidence"]`` in the
    shared module. Stdout carries a one-line summary of the status counts.

Exit codes:
    0 every entry produced an evidence record; 1 at least one entry failed (git error or malformed entry, named on
    stderr; the other records are still written); 2 the repository or the entries file is unusable.

Evidence statuses:
    ``unlinked`` - the entry has no commits. ``unattributable`` - no source line could be blamed after exclusions.
    ``timed_out`` - the per-fix budget or a git call timeout was hit; partial results are discarded and the fix is
    retried on a later run. ``ok`` - at least one line was blamed.

Rules worth knowing:
    * A file is blamed only when its pre-fix (old-side) path is a source path. ``files`` lists every touched path
      that is not excluded: the new path, plus the old path of a rename or deletion.
    * Lines blamed to one of the fix's own commits (a later commit of a multi-commit fix touching an earlier one)
      are dropped: a fix is never its own origin.
    * Each blamed range is padded by a few lines because ``-M``/``-C`` only recognise moved blocks above a minimum
      size; the padding lines are blamed but never counted.
    * The repository's own ``blame.ignoreRevsFile`` setting is cleared; only the ``.git-blame-ignore-revs`` file in
      the fix's base tree is honoured, so the evidence fingerprint captures everything that shapes the result.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Final

from _evolution_common import (
    ALGO_VERSIONS,
    GIT_TIMEOUT_S,
    SCHEMA_VERSION,
    GitError,
    GitTimeout,
    canonical_json,
    fingerprint,
    python_symbols_changed,
    run_git,
)

#: Changelog and news file stems; matched at any depth, bare or with a ``.txt`` suffix (``.md``/``.rst`` and
#: ``.adoc`` are covered by the docs globs). Source modules such as ``history.py`` stay included.
_CHANGELOG_STEMS: Final = (
    "CHANGELOG",
    "ChangeLog",
    "Changelog",
    "changelog",
    "CHANGES",
    "Changes",
    "changes",
    "HISTORY",
    "History",
    "history",
    "NEWS",
    "News",
    "news",
)

#: Default exclusion globs: tests, docs, changelog/news files, lock files and generated code. ``**`` spans zero or
#: more whole path segments, ``*`` never crosses ``/``; see :func:`glob_match`.
DEFAULT_EXCLUDES: Final[tuple[str, ...]] = (
    # tests
    "tests/**",
    "test/**",
    "**/tests/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/conftest.py",
    # docs
    "docs/**",
    "doc/**",
    "*.md",
    "*.rst",
    "*.txt",
    "**/*.md",
    "**/*.rst",
    "**/*.adoc",
    # changelog and news files, towncrier fragment directories
    *(f"**/{stem}{suffix}" for stem in _CHANGELOG_STEMS for suffix in ("", ".txt")),
    "newsfragments/**",
    "**/newsfragments/**",
    "changelog.d/**",
    "**/changelog.d/**",
    # lock files
    "*.lock",
    "**/*.lock",
    "package-lock.json",
    "**/package-lock.json",
    "**/npm-shrinkwrap.json",
    "**/pnpm-lock.yaml",
    "**/go.sum",
    # generated code
    "**/*_pb2.py",
    "**/*_pb2_grpc.py",
    "**/*.min.js",
    "**/*.min.css",
)

#: Blame flags: ignore whitespace, follow moved lines inside a file and lines moved or copied across files.
BLAME_FLAGS: Final = ("-w", "-M", "-C")

#: Context lines blamed on each side of an add-only hunk's insertion point.
CONTEXT_LINES: Final = 3

#: Lines added on each side of every blamed range so ``-M``/``-C`` see a block large enough to recognise a move
#: (git needs about 20 alphanumeric characters for ``-M`` and 40 for ``-C``); padding lines are never counted.
BLAME_PAD_LINES: Final = 5

#: Ignore-revs file read from the fix's base tree.
IGNORE_REVS_FILE: Final = ".git-blame-ignore-revs"

#: Diff flags that make the output independent of the user's git configuration.
_DIFF_FLAGS: Final = (
    "-U0",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--diff-algorithm=myers",
    "-M",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)

_HEX = re.compile(r"^[0-9a-fA-F]{4,64}$")
_HUNK = re.compile(r"^@@ -(?P<start>\d+)(?:,(?P<count>\d+))? \+\d+(?:,\d+)? @@")
_PORCELAIN_HEAD = re.compile(r"^(?P<sha>[0-9a-f]{40}|[0-9a-f]{64}) \d+ (?P<final>\d+)(?: \d+)?$")
_GITLINK_HEADERS: Final = ("index ", "new file mode ", "deleted file mode ", "old mode ", "new mode ")
_C_ESCAPES: Final = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


class EvidenceStatus(str, Enum):
    """Outcome of collecting blame evidence for one fix."""

    OK = "ok"
    UNATTRIBUTABLE = "unattributable"
    TIMED_OUT = "timed_out"
    UNLINKED = "unlinked"


class _BudgetExceeded(Exception):
    """Raised when a fix's wall-clock evidence budget is spent."""


# --------------------------------------------------------------------------------------------------------------------
# Path globs
# --------------------------------------------------------------------------------------------------------------------


def _segments(text: str) -> list[str]:
    """Split a POSIX-style path or pattern into segments, dropping empty and ``.`` segments."""
    return [seg for seg in text.strip().replace("\\", "/").split("/") if seg not in ("", ".")]


def _segments_match(pattern: list[str], path: list[str]) -> bool:
    """Match path segments against pattern segments; ``**`` consumes zero or more whole segments."""
    reachable = {0}
    for seg in pattern:
        if seg == "**":
            reachable = set(range(min(reachable), len(path) + 1))
        else:
            reachable = {pos + 1 for pos in reachable if pos < len(path) and fnmatchcase(path[pos], seg)}
        if not reachable:
            return False
    return len(path) in reachable


def glob_match(pattern: str, path: str) -> bool:
    """Return True when a repository path matches a glob with ``**`` support.

    Semantics: the pattern is anchored at the repository root; ``*``, ``?`` and ``[...]`` match within one path
    segment (case-sensitive, never across ``/``); a ``**`` segment matches zero or more whole segments; a trailing
    ``/`` means everything below that directory. Backslashes are read as ``/``.

    Args:
        pattern: Glob such as ``**/tests/**`` or ``*.md``.
        path: Repository-relative POSIX path as git reports it.

    Examples:
        >>> glob_match("*.md", "README.md"), glob_match("*.md", "docs/guide.md")
        (True, False)
        >>> glob_match("**/*.md", "README.md"), glob_match("src/**/util.py", "src/a/b/util.py")
        (True, True)
    """
    segments = _segments(pattern)
    if pattern.rstrip().endswith(("/", "\\")):
        segments.append("**")
    return bool(segments) and _segments_match(segments, _segments(path))


def is_excluded(path: str, globs: tuple[str, ...] | list[str]) -> bool:
    """Return True when a repository path matches any exclusion glob.

    Examples:
        >>> is_excluded("pkg/tests/test_io.py", DEFAULT_EXCLUDES), is_excluded("pkg/io.py", DEFAULT_EXCLUDES)
        (True, False)
    """
    return any(glob_match(pattern, path) for pattern in globs)


# --------------------------------------------------------------------------------------------------------------------
# Entry validation and commit planning
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _CommitPlan:
    """One fix commit resolved to its full SHA, its diff base and the base's ignore-revs blob.

    Attributes:
        sha: Full commit SHA.
        base: First parent (the blame revision), or None for a root commit.
        ignore_revs: Blob ID of ``.git-blame-ignore-revs`` in the base tree, or None when absent.
    """

    sha: str
    base: str | None
    ignore_revs: str | None


def _fix_id(entry: Any) -> str:
    """Return the entry's ``entry_id``, raising ValueError for anything but a non-empty string."""
    fix_id = entry.get("entry_id") if isinstance(entry, dict) else None
    if not isinstance(fix_id, str) or not fix_id:
        raise ValueError("fix entry needs a non-empty 'entry_id' string; pass entries from build_entries()['fixes']")
    return fix_id


def _entry_commits(entry: dict[str, Any], fix_id: str) -> list[str]:
    """Return the entry's commits, lower-cased and de-duplicated in order.

    Every commit must be a hex object name: anything else could reach git as an option, so it is refused.
    """
    commits = entry.get("commits")
    if not isinstance(commits, list) or not all(isinstance(sha, str) and _HEX.match(sha) for sha in commits):
        raise ValueError(f"fix entry {fix_id!r}: 'commits' must be a list of hex commit SHAs")
    return list(dict.fromkeys(sha.lower() for sha in commits))


def _ignore_revs_blobs(repo: Path, bases: list[str], timeout: float) -> dict[str, str | None]:
    """Map each base commit to the blob ID of its ``.git-blame-ignore-revs`` (None when absent), in one git call."""
    if not bases:
        return {}
    query = "".join(f"{base}:{IGNORE_REVS_FILE}\n" for base in bases)
    out = run_git(repo, "cat-file", "--batch-check", timeout=timeout, stdin=query).splitlines()
    blobs: dict[str, str | None] = {}
    for base, line in zip(bases, out):
        parts = line.split()
        blobs[base] = parts[0] if len(parts) >= 2 and parts[1] == "blob" else None
    return blobs


def _plan_commits(repo: Path, commits: list[str], timeout: float) -> list[_CommitPlan]:
    """Resolve commits to full SHAs with their first parents and ignore-revs blobs, de-duplicated by full SHA."""
    resolved: dict[str, str | None] = {}
    for sha in commits:
        parts = run_git(repo, "rev-list", "--parents", "-n", "1", sha, "--", timeout=timeout).split()
        if not parts:
            raise GitError(f"commit {sha} did not resolve; check that the entry belongs to this repository")
        resolved.setdefault(parts[0], parts[1] if len(parts) > 1 else None)
    bases = sorted({base for base in resolved.values() if base is not None})
    blobs = _ignore_revs_blobs(repo, bases, timeout)
    return [_CommitPlan(sha, base, blobs.get(base) if base else None) for sha, base in resolved.items()]


def _fingerprint_from(plans: list[_CommitPlan], exclude_globs: tuple[str, ...]) -> str:
    """Fingerprint everything the evidence of one fix depends on."""
    commits = [{"sha": plan.sha, "base": plan.base, "ignore_revs": plan.ignore_revs} for plan in plans]
    return fingerprint(
        "evidence",
        ALGO_VERSIONS["evidence"],
        list(BLAME_FLAGS),
        CONTEXT_LINES,
        BLAME_PAD_LINES,
        sorted(commits, key=lambda item: item["sha"]),
        sorted(set(exclude_globs)),
    )


def evidence_fingerprint(
    repo: Path,
    fix_entry: dict[str, Any],
    exclude_globs: tuple[str, ...] | list[str] = DEFAULT_EXCLUDES,
    *,
    timeout: float = GIT_TIMEOUT_S,
) -> str:
    """Return the evidence-stage fingerprint of one fix entry.

    It covers the fix's commits (as full SHAs, order-independent), the blame flags and padding, the context width,
    the sorted exclusion globs, the ``.git-blame-ignore-revs`` blob at each commit's base and the evidence algorithm
    version. Evidence never depends on columns, so remapping columns never changes it.

    Args:
        repo: Repository root.
        fix_entry: Fix entry with ``entry_id`` and ``commits``.
        exclude_globs: Exclusion globs used for the evidence.
        timeout: Per-git-call timeout in seconds.

    Raises:
        ValueError: the entry has no ``entry_id`` or a commit that is not a hex SHA.
        GitError: a commit does not resolve (``GitTimeout`` when git times out).

    Examples:
        >>> evidence_fingerprint(Path("."), {"entry_id": "1.0.0/abc", "commits": []}) == evidence_fingerprint(
        ...     Path("."), {"entry_id": "1.0.0/def", "commits": []}
        ... )
        True
    """
    fix_id = _fix_id(fix_entry)
    plans = _plan_commits(Path(repo), _entry_commits(fix_entry, fix_id), timeout)
    return _fingerprint_from(plans, tuple(exclude_globs))


# --------------------------------------------------------------------------------------------------------------------
# Diff parsing
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class _FileChange:
    """Old-side view of one file in a ``-U0`` diff.

    Attributes:
        old_path: Path at the base commit, or None for an added file.
        gitlink: True for a submodule entry, which has no blamable lines.
        modified: Inclusive old-side line ranges modified or deleted by the commit.
        inserts: Old-side line numbers after which add-only hunks insert lines (0 = top of file).
    """

    old_path: str | None = None
    gitlink: bool = False
    modified: list[tuple[int, int]] = field(default_factory=list)
    inserts: list[int] = field(default_factory=list)


def _c_unquote(body: str) -> str:
    """Decode the inside of a C-style quoted path as git writes it (escapes and octal bytes)."""
    out = bytearray()
    pos = 0
    while pos < len(body):
        char = body[pos]
        nxt = body[pos + 1 : pos + 2]
        if char == "\\" and nxt in _C_ESCAPES:
            out.append(_C_ESCAPES[nxt])
            pos += 2
        elif char == "\\" and re.fullmatch(r"[0-7]{3}", body[pos + 1 : pos + 4]):
            out.append(int(body[pos + 1 : pos + 4], 8))
            pos += 4
        else:
            out += char.encode("utf-8")
            pos += 1
    return out.decode("utf-8", errors="replace")


def _diff_path(raw: str, prefix: str) -> str | None:
    """Return the path from a ``---``/``+++`` header value, or None for ``/dev/null``."""
    text = raw[:-1] if raw.endswith("\t") else raw  # git appends a tab when the name contains a space
    if text == "/dev/null":
        return None
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = _c_unquote(text[1:-1])
    return text[len(prefix) :] if text.startswith(prefix) else text


def _add_hunk(change: _FileChange, match: re.Match[str]) -> None:
    """Record one hunk header on the old side: a modified range, or an insertion point for an add-only hunk."""
    start = int(match.group("start"))
    count = int(match.group("count")) if match.group("count") is not None else 1
    if count == 0:
        change.inserts.append(start)
    else:
        change.modified.append((start, start + count - 1))


def _parse_diff(text: str) -> list[_FileChange]:
    """Parse ``git diff -U0`` output into per-file old-side changes.

    Header lines are read only between ``diff --git`` and the first hunk, so a removed line that itself starts with
    ``--`` is never mistaken for a file header.
    """
    changes: list[_FileChange] = []
    current: _FileChange | None = None
    in_header = False
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            current = _FileChange()
            changes.append(current)
            in_header = True
        elif current is None:
            continue
        elif (match := _HUNK.match(line)) is not None:
            in_header = False
            _add_hunk(current, match)
        elif in_header and line.startswith("--- "):
            current.old_path = _diff_path(line[4:], "a/")
        elif in_header and line.startswith(_GITLINK_HEADERS) and line.endswith(" 160000"):
            current.gitlink = True
    return changes


def _parse_name_status(text: str) -> list[str]:
    """Return every path touched in ``git diff --name-status -z`` output (both sides of a rename)."""
    tokens = text.split("\0")
    paths: list[str] = []
    pos = 0
    while pos < len(tokens) and tokens[pos]:
        status = tokens[pos]
        if status[:1] == "R":
            paths += [tokens[pos + 1], tokens[pos + 2]]
            pos += 3
        elif status[:1] == "C":
            paths.append(tokens[pos + 2])  # a copy leaves its source untouched
            pos += 3
        else:
            paths.append(tokens[pos + 1])
            pos += 2
    return paths


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or adjacent inclusive line ranges."""
    merged: list[tuple[int, int]] = []
    for low, high in sorted(ranges):
        if merged and low <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def _context_ranges(inserts: list[int]) -> list[tuple[int, int]]:
    """Return the context ranges around insertion points: lines ``p-2..p`` before and ``p+1..p+3`` after.

    The upper bound may run past the end of the file; ``git blame -L`` clamps it.
    """
    return [(max(1, point - CONTEXT_LINES + 1), point + CONTEXT_LINES) for point in inserts]


def _line_set(ranges: list[tuple[int, int]]) -> set[int]:
    """Expand inclusive ranges into the set of line numbers they cover."""
    return {line for low, high in ranges for line in range(low, high + 1)}


def _parse_porcelain(text: str) -> list[tuple[str, int]]:
    """Return ``(blamed sha, final line number)`` for each line of ``git blame --porcelain`` output."""
    lines: list[tuple[str, int]] = []
    current: tuple[str, int] | None = None
    for line in text.split("\n"):
        if line.startswith("\t"):
            if current is not None:
                lines.append(current)
            continue
        match = _PORCELAIN_HEAD.match(line)
        if match is not None:
            current = (match.group("sha"), int(match.group("final")))
    return lines


def _rev_token(line: str) -> str | None:
    """Return the object name on one ignore-revs line (comments and blanks give None)."""
    token = line.split("#", 1)[0].strip().split(" ", 1)[0]
    return token.lower() if _HEX.match(token) else None


# --------------------------------------------------------------------------------------------------------------------
# Evidence collection
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class _Budget:
    """Wall-clock budget of one fix; every git call checks it first and caps its timeout to what remains."""

    timeout: float
    deadline: float | None

    @classmethod
    def start(cls, timeout: float, budget_s: float | None) -> _Budget:
        """Start the clock; ``budget_s`` None means unlimited."""
        return cls(timeout, None if budget_s is None else time.monotonic() + budget_s)

    def call_timeout(self) -> float:
        """Return the timeout for the next git call, raising _BudgetExceeded once the budget is spent."""
        if self.deadline is None:
            return self.timeout
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _BudgetExceeded
        return min(self.timeout, remaining)


@dataclass
class _Tally:
    """Evidence accumulated over a fix's commits."""

    blamed: Counter[str] = field(default_factory=Counter)
    context: Counter[str] = field(default_factory=Counter)
    files: set[str] = field(default_factory=set)
    symbols: set[str] = field(default_factory=set)


@dataclass
class _BlameRun:
    """Collects blame evidence for one fix: shared inputs, budget, ignore-revs cache and the running tally."""

    repo: Path
    globs: tuple[str, ...]
    budget: _Budget
    own: frozenset[str]
    tmp_dir: Path
    tally: _Tally = field(default_factory=_Tally)
    revs_files: dict[str, Path | None] = field(default_factory=dict)

    def git(self, *args: str, stdin: str | None = None) -> str:
        """Run one git command under the budget."""
        return run_git(self.repo, *args, timeout=self.budget.call_timeout(), stdin=stdin)

    def ignore_file(self, blob: str | None) -> Path | None:
        """Write the base's ignore-revs list, keeping only names that resolve to commits; None when nothing remains.

        Unknown or abbreviated names would make ``git blame`` fail, so each one is resolved first and written back as a
        full commit SHA.
        """
        if blob is None:
            return None
        if blob not in self.revs_files:
            tokens = [tok for tok in map(_rev_token, self.git("cat-file", "blob", blob).splitlines()) if tok]
            query = "".join(f"{tok}^{{commit}}\n" for tok in tokens)
            found = self.git("cat-file", "--batch-check", stdin=query).splitlines() if tokens else []
            shas = [parts[0] for parts in map(str.split, found) if len(parts) >= 2 and parts[1] == "commit"]
            path = self.tmp_dir / f"{blob}.revs" if shas else None
            if path is not None:
                path.write_text("".join(f"{sha}\n" for sha in shas), encoding="utf-8", newline="\n")
            self.revs_files[blob] = path
        return self.revs_files[blob]

    def blame(
        self,
        base: str,
        ignore_blob: str | None,
        path: str,
        modified: list[tuple[int, int]],
        context: list[tuple[int, int]],
    ) -> None:
        """Blame the modified and context lines of one file at the base in a single call and tally them.

        The empty ``--ignore-revs-file`` argument clears any ``blame.ignoreRevsFile`` from the repository's own
        configuration before the base tree's list is added.
        """
        padded = [(max(1, low - BLAME_PAD_LINES), high + BLAME_PAD_LINES) for low, high in modified + context]
        args = ["-c", "diff.algorithm=myers", "blame", "--porcelain", *BLAME_FLAGS, "--ignore-revs-file", ""]
        revs = self.ignore_file(ignore_blob)
        if revs is not None:
            args += ["--ignore-revs-file", str(revs)]
        for low, high in _merge_ranges(padded):
            args += ["-L", f"{low},{high}"]
        out = self.git(*args, base, "--", path)
        modified_lines, context_lines = _line_set(modified), _line_set(context)
        for sha, line in _parse_porcelain(out):
            if sha in self.own:
                continue
            if line in modified_lines:
                self.tally.blamed[sha] += 1
            elif line in context_lines:
                self.tally.context[sha] += 1

    def _file_is_empty(self, base: str, path: str) -> bool:
        """Return True when the file at the base has no content (and therefore no line to blame)."""
        return int(self.git("cat-file", "-s", f"{base}:{path}").strip() or 0) == 0

    def commit(self, plan: _CommitPlan) -> None:
        """Tally one commit: touched files, blamed lines and changed Python symbols.

        Root commits add nothing.
        """
        if plan.base is None:
            return
        touched = _parse_name_status(self.git("diff", "--name-status", "-z", "-M", plan.base, plan.sha))
        self.tally.files.update(path for path in touched if not is_excluded(path, self.globs))
        diff = self.git("-c", "core.quotePath=false", "diff", *_DIFF_FLAGS, plan.base, plan.sha)
        for change in _parse_diff(diff):
            if change.old_path is None or change.gitlink or is_excluded(change.old_path, self.globs):
                continue
            inserts = change.inserts
            if 0 in inserts and self._file_is_empty(plan.base, change.old_path):
                inserts = []
            context = _context_ranges(inserts)
            if change.modified or context:
                self.blame(plan.base, plan.ignore_revs, change.old_path, change.modified, context)
        symbols = python_symbols_changed(self.repo, plan.sha, timeout=self.budget.call_timeout())
        self.tally.symbols.update(sym for sym in symbols if not is_excluded(sym.rsplit(":", 1)[0], self.globs))


def _evidence_record(fix_id: str, status: EvidenceStatus, fp: str, tally: _Tally | None = None) -> dict[str, Any]:
    """Build the JSON evidence record; a missing tally gives empty evidence."""
    tally = tally if tally is not None else _Tally()
    return {
        "schema_version": SCHEMA_VERSION,
        "fix_id": fix_id,
        "status": status.value,
        "blamed": dict(sorted(tally.blamed.items())),
        "context_blamed": dict(sorted(tally.context.items())),
        "files": sorted(tally.files),
        "symbols": sorted(tally.symbols),
        "fingerprint": fp,
    }


def collect_evidence(
    repo: Path,
    fix_entry: dict[str, Any],
    *,
    exclude_globs: tuple[str, ...] | list[str] = DEFAULT_EXCLUDES,
    timeout: float = GIT_TIMEOUT_S,
    budget_s: float | None = None,
) -> dict[str, Any]:
    """Blame the lines a fix changed and return its evidence record.

    Each commit is diffed against its first parent (merges too); a root commit contributes nothing. Modified and
    deleted old-side lines of non-excluded files are blamed into ``blamed``; add-only hunks blame up to
    :data:`CONTEXT_LINES` lines on each side of the insertion point into ``context_blamed``. The fingerprint is
    computed before the budget starts, so a ``timed_out`` record still carries it (an empty string only when the
    fingerprint's own git calls time out).

    Args:
        repo: Repository root.
        fix_entry: Fix entry with ``entry_id`` and ``commits`` (hex SHAs).
        exclude_globs: Paths whose lines are never blamed and that never appear in ``files`` or ``symbols``.
        timeout: Per-git-call timeout in seconds.
        budget_s: Wall-clock budget for the fix in seconds; None is unlimited and 0 times out at once.

    Returns:
        Evidence record with the keys of ``RECORD_FIELDS["evidence"]``.

    Raises:
        ValueError: the entry has no ``entry_id`` or a commit that is not a hex SHA.
        GitError: a git command failed for a reason other than a timeout (for example an unknown commit).

    Examples:
        >>> collect_evidence(Path("."), {"entry_id": "1.0.0/abc", "commits": []})["status"]
        'unlinked'
    """
    fix_id = _fix_id(fix_entry)
    commits = _entry_commits(fix_entry, fix_id)
    globs = tuple(exclude_globs)
    if not commits:
        return _evidence_record(fix_id, EvidenceStatus.UNLINKED, _fingerprint_from([], globs))
    root = Path(repo)
    try:
        plans = _plan_commits(root, commits, timeout)
    except GitTimeout:
        return _evidence_record(fix_id, EvidenceStatus.TIMED_OUT, "")
    fp = _fingerprint_from(plans, globs)
    budget = _Budget.start(timeout, budget_s)
    try:
        with tempfile.TemporaryDirectory(prefix="evolution-blame-") as tmp:
            run = _BlameRun(root, globs, budget, frozenset(plan.sha for plan in plans), Path(tmp))
            for plan in plans:
                run.commit(plan)
        budget.call_timeout()  # a fix that finished past its budget is over budget too
    except (GitTimeout, _BudgetExceeded):
        return _evidence_record(fix_id, EvidenceStatus.TIMED_OUT, fp)
    attributable = bool(run.tally.blamed or run.tally.context)
    status = EvidenceStatus.OK if attributable else EvidenceStatus.UNATTRIBUTABLE
    return _evidence_record(fix_id, status, fp, run.tally)


# --------------------------------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------------------------------


def _load_entries(path: Path) -> list[dict[str, Any]]:
    """Read fix entries: a JSON list, or a ``build_entries`` result whose ``fixes`` key holds the list."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "fixes" in data:
        data = data["fixes"]
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError(f"{path}: expected a JSON list of fix entries or a build_entries result with 'fixes'")
    return data


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="evolution_blame.py",
        description="Collect SZZ blame evidence per fix entry for /oss:analyse evolution.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    evidence = commands.add_parser("evidence", help="blame every fix entry and write evidence JSONL")
    evidence.add_argument("--repo", type=Path, required=True, help="repository root")
    evidence.add_argument("--entries", type=Path, required=True, help="JSON fix entries (list or build_entries)")
    evidence.add_argument("--out", type=Path, required=True, help="evidence JSONL to write (replaced)")
    evidence.add_argument("--budget-s", type=float, default=None, help="wall-clock budget per fix (default: none)")
    evidence.add_argument("--timeout", type=float, default=GIT_TIMEOUT_S, help="timeout per git call in seconds")
    evidence.add_argument(
        "--exclude", action="append", default=None, metavar="GLOB", help="exclusion glob; replaces the defaults"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line; see the module docstring for arguments and exit codes."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0 or (args.budget_s is not None and args.budget_s < 0):
        parser.error("--timeout must be positive and --budget-s non-negative")
    if not args.repo.is_dir():
        print(f"evolution_blame: repository {args.repo} is not a directory", file=sys.stderr)
        return 2
    try:
        entries = _load_entries(args.entries)
    except (OSError, ValueError) as exc:
        print(f"evolution_blame: cannot read entries: {exc}", file=sys.stderr)
        return 2
    globs = tuple(args.exclude) if args.exclude else DEFAULT_EXCLUDES
    started = time.monotonic()
    records: list[dict[str, Any]] = []
    failed = 0
    for entry in entries:
        try:
            records.append(
                collect_evidence(args.repo, entry, exclude_globs=globs, timeout=args.timeout, budget_s=args.budget_s)
            )
        except (GitError, ValueError) as exc:
            failed += 1
            print(f"evolution_blame: entry skipped: {exc}", file=sys.stderr)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(f"{canonical_json(record)}\n" for record in records), encoding="utf-8", newline="\n")
    counts = Counter(record["status"] for record in records)
    summary = " ".join(f"{status.value}={counts.get(status.value, 0)}" for status in EvidenceStatus)
    elapsed = time.monotonic() - started
    print(f"evidence: {len(records)} records ({summary}) failed={failed} in {elapsed:.1f}s -> {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
