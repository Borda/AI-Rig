#!/usr/bin/env python
"""Find and parse a repository's changelog, then turn every release into linked fix rows and feature columns.

Purpose: Stage two of ``/oss:analyse evolution``. :func:`discover_changelog` locates the changelog file (and any
towncrier setup), :func:`parse_changelog` splits it into per-version Added/Changed/Fixed bullets, and
:func:`build_entries` produces the fix rows and feature/change columns the scorer consumes. Each entry is linked to
commits through a fixed ladder (first hit wins): explicit SHA, PR number (local commit messages first, then GitHub),
issue number (closing PR through GitHub), text match against commit subjects in the release range, else unlinked.
Releases without a changelog section fall back to conventional-commit prefixes (and PR labels when ``gh`` works).

Usage:
    evolution_changelog.py discover --repo DIR
    evolution_changelog.py entries --repo DIR --index FILE --out FILE [--no-gh]

Outputs:
    ``discover`` prints one JSON object: ``{"path", "format", "towncrier"}``; ``path`` is null when no changelog
    exists (a default path is never invented).
    ``entries`` reads the release index JSON written by ``evolution_releases.py``, writes
    ``{"fixes", "columns", "aliases", "changelog", "warnings"}`` to ``--out`` and prints a one-line summary.

Exit codes:
    0 success; 1 invalid arguments, unreadable or malformed index; 2 a git command failed or timed out.

Changelog files are searched in ``./``, ``docs/`` and ``docs/source/`` (that order). Inside one directory the stem
decides first (``CHANGELOG`` > ``CHANGES`` > ``HISTORY`` > ``NEWS``, any case, exact stem before a suffixed one such
as ``CHANGELOG-old``), then the extension (``.md`` > ``.rst`` > ``.txt`` > none), then the file name. Changelog text,
commit messages and GitHub data are untrusted: they are parsed as data only, never executed or used as paths.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from shutil import which
from typing import Any, Final, Literal, Protocol

#: Sibling import: bin/ is not a package. Putting it on the path resolves the shared module (and the optional
#: ``gh_batch_query`` helper) whether this file runs as a script, under the test conftest, or as a doctest module.
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from _evolution_common import (  # noqa: E402
    DEFAULT_BUG_LABELS,
    MAX_ENTRY_LINE_CHARS,
    SOURCE_CHANGELOG,
    SOURCE_HEURISTIC,
    GitError,
    atomic_write_json,
    canonical_json,
    commit_id,
    entry_hash,
    entry_ids,
    first_parent,
    normalise_entry_text,
    parse_remote_url,
    parse_version,
    python_symbols_changed,
    read_bounded_text,
    run_git,
)

# --------------------------------------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------------------------------------

#: Changelog stems in precedence order; matched case-insensitively, exactly or followed by a separator.
_STEMS: Final = ("changelog", "changes", "history", "news")
#: Accepted extensions in precedence order with the parser format each selects ("" = no extension, plain text).
_EXTENSIONS: Final = {".md": "md", ".rst": "rst", ".txt": "txt", "": "txt"}
#: Directories searched for a changelog, in precedence order ("" = repository root).
_SEARCH_DIRS: Final = ("", "docs", "docs/source")
#: Towncrier fragment directories tried when the config names none.
_FRAGMENT_DIRS: Final = ("newsfragments", "changes", "changelog.d", "news")
#: Towncrier fragment types mapped to changelog buckets; other types (doc, misc) carry no behaviour.
_FRAGMENT_BUCKET: Final = {
    "feature": "added",
    "feat": "added",
    "bugfix": "fixed",
    "fix": "fixed",
    "bug": "fixed",
    "removal": "changed",
    "deprecation": "changed",
    "breaking": "changed",
    "change": "changed",
}
#: Changelog bucket of each entry kind, and the reverse.
_BUCKETS: Final = ("added", "changed", "fixed")
_KIND_OF_BUCKET: Final = {"added": "feature", "changed": "change", "fixed": "fix"}
#: Name of the pseudo-release holding entries not yet tagged.
UNRELEASED: Final = "unreleased"
#: Link confidence per ladder rung; text and heuristic values are caps.
_CONFIDENCE_EXACT: Final = 1.0
_CONFIDENCE_TEXT_CAP: Final = 0.4
_CONFIDENCE_HEURISTIC: Final = 0.5
#: Most side-branch commits one merge commit may add to a column; a longer side branch is a branch merge, not a PR.
_MERGED_LIMIT: Final = 100
#: Text match acceptance: at least this many shared tokens covering at least this share of the entry's tokens.
_TEXT_MIN_SHARED: Final = 2
_TEXT_MIN_COVERAGE: Final = 0.5
#: Aliases per GitHub GraphQL request.
_GH_BATCH: Final = 25
#: Attempts per lookup and the pause that grows with each one; only a network-level ``gh`` failure is retried.
_GH_ATTEMPTS: Final = 3
_GH_BACKOFF_SECONDS: Final = 2.0
_GH_NETWORK_FAILURE: Final = "gh api graphql exited"
#: Versions named in one summary warning before it is cut short with "...".
_WARN_LIST_MAX: Final = 5
#: Timeout for ``git log`` over a whole release range (larger than the per-call default).
_LOG_TIMEOUT_S: Final = 300
#: Conventional-commit types mapped to entry kinds; other types are not fixes or features.
_CONVENTIONAL_KIND: Final = {"fix": "fix", "bugfix": "fix", "hotfix": "fix", "feat": "feature", "feature": "feature"}
#: PR labels (lower case) that classify an otherwise unprefixed commit.
_FIX_LABELS: Final = frozenset({*DEFAULT_BUG_LABELS, "fix", "bugfix", "regression"})
_FEATURE_LABELS: Final = frozenset({"feature", "enhancement", "type: feature", "kind/feature", "type/feature"})
#: Labels and title shapes of a change that adds no capability; a pull request carrying them is not a feature column.
_REFACTOR_LABELS: Final = frozenset(
    {"refactor", "refactoring", "cleanup", "chore", "maintenance", "housekeeping", "type: refactor", "kind/cleanup"}
)
_REFACTOR_TITLE: Final = re.compile(
    r"\b(?:refactor\w*|clean[- ]?ups?|tidy\w*|rename[ds]?|reformat\w*|remove unused|replace\b.{1,40}?\bwith)\b",
    re.IGNORECASE,
)
#: Words too common to make two texts look related.
_STOPWORDS: Final = frozenset(
    "the and for with from into when that this than then not are was were has have its now all any can fix fixed "
    "fixes fixing add added adds adding update updated updates change changed changes remove removed feat feature "
    "bug issue use used using new make makes made more less via also been will should which where merge pull "
    "request".split()
)

_SHA_FULL = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_SHA_TOKEN = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")
_HASH_REF = re.compile(r"(?<![\w/&#])#(\d{1,7})\b")
_GH_REF = re.compile(r"\bgh-(\d{1,7})\b", re.IGNORECASE)
_URL_REF = re.compile(r"https?://[^\s)\]>]+?/(?P<slug>[\w.-]+/[\w.-]+)/(?P<kind>pull|issues)/(?P<num>\d{1,7})\b")
_SQUASH_PR = re.compile(r"\(#(?P<n>\d{1,7})\)\s*$")
#: ``Reapply "<original subject>"`` as written by ``git revert`` of a revert; the quoted subject ends in the PR number.
_REAPPLY = re.compile(r'^Reapply "(?P<inner>.+)"(?:\s+\(#\d{1,7}\))?\s*$')
_MERGE_PR = re.compile(r"^Merge pull request #(?P<n>\d{1,7})\b")
_CONVENTIONAL = re.compile(r"^(?P<type>[A-Za-z]+)(?:\([^)]*\))?!?:\s*(?P<rest>.*)$")
_REGRESSION = re.compile(
    r"\b(?:since|worked in|working in|works in|regression (?:in|since|from)|regressed in|broken (?:in|since)|"
    r"broke in|introduced in)\s+(?:version\s+|release\s+)?(?P<v>[vV]?\d+(?:\.\d+)+[0-9A-Za-z.+-]*)",
    re.IGNORECASE,
)
_VERSION_TOKEN = re.compile(
    r"(?<![\w.])[vV]?\d+(?:\.\d+)+(?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?\d*)?"
    r"(?:[-_.]?(?:post|rev)[-_.]?\d*)?(?:[-_.]?dev[-_.]?\d*)?(?!\w)",
    re.IGNORECASE,
)
_UNRELEASED_WORD = re.compile(r"\bunreleased\b", re.IGNORECASE)
#: Heading GitHub puts over its generated release notes: a mixed list, never one kind of change.
_WHATS_CHANGED = re.compile(r"what['\u2019]?s changed", re.IGNORECASE)
#: Most release lists the client reads: 100 per page, up to 50 pages.
_GH_RELEASE_PAGES: Final = 50
_ATX = re.compile(r"^ {0,3}(?P<hashes>#{1,6})\s+(?P<text>.*)")
_SETEXT = re.compile(r"^ {0,3}(?:={3,}|-{3,})\s*$")
_FENCE = re.compile(r"^ {0,3}(?:```|~~~)")
_ADORNMENT = re.compile(r"^([!-/:-@\[-`{-~])\1{2,}\s*$")
_BULLET = re.compile(r"^(?P<indent>\s*)(?:[-*+•]|\d+[.)]|#\.)\s+(?P<text>\S.*)$")
_PSEUDO_HEADING = re.compile(
    r"^(?:\*\*|__)(?P<a>[^*_\n]{2,40}?)\s*:?\s*(?:\*\*|__)\s*:?\s*$|^(?P<b>[A-Za-z][A-Za-z &/-]{1,40}):\s*$"
)
_SUBSECTION_RULES: Final = (
    ("fixed", re.compile(r"\b(?:fix(?:ed|es)?|bug ?fix(?:es)?|bugs?|security)\b")),
    ("changed", re.compile(r"\b(?:deprecat\w*|remov\w*|breaking)\b")),
    ("added", re.compile(r"\b(?:add(?:ed|s|itions?)?|new|features?)\b")),
    ("changed", re.compile(r"\b(?:chang\w*|improv\w*|enhancement\w*|performance)\b")),
)
#: Leading words that classify a bullet written outside any subsection, tried in this order; no match drops the bullet.
_FLAT_RULES: Final = (
    (
        "fixed",
        re.compile(
            r"^(?:fix(?:e[sd])?|bug ?fix(?:es)?|resolv(?:e|es|ed)|correct(?:s|ed)?|repair(?:s|ed)?|patch(?:es|ed)?)\b"
            r"|\bno longer\b",
            re.IGNORECASE,
        ),
    ),
    (
        "added",
        re.compile(
            r"^(?:add(?:s|ed)?|introduc(?:e|es|ed)|new|support(?:s|ed)?|implement(?:s|ed)?|provide[sd]?"
            r"|enabl(?:e|es|ed)|allow(?:s|ed)?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "changed",
        re.compile(
            r"^(?:chang(?:e|es|ed)|update[sd]?|improv(?:e|es|ed)|remov(?:e|es|ed)|deprecat(?:e|es|ed)"
            r"|renam(?:e|es|ed)|replac(?:e|es|ed)|switch(?:es|ed)?|mov(?:e|es|ed)|refactor(?:s|ed)?|rework(?:s|ed)?"
            r"|drop(?:s|ped)?)\b|^(?:\S+\s+){0,11}now\b|\binstead of\b",
            re.IGNORECASE,
        ),
    ),
)
#: Towncrier fragment ``<ref>.<type>[.<counter>][.<ext>]``; an extension alone (``notes.txt``) is not a type.
_FRAGMENT_NAME = re.compile(
    r"^(?P<ref>[^.]+)\.(?P<type>(?!(?:md|rst|txt)$)[A-Za-z]+)(?:\.\d+)?(?:\.(?:md|rst|txt))?\Z", re.IGNORECASE
)
_TOWNCRIER_HEADER = re.compile(r"^\s*\[tool\.towncrier\]\s*$")
_TOML_HEADER = re.compile(r"^\s*\[")
_TOML_STRING = re.compile(r"^\s*(?P<key>directory|filename)\s*=\s*[\"'](?P<value>[^\"']+)[\"']")
_LOG_FORMAT: Final = "%H%x00%P%x00%s%x00%b%x1e"

Buckets = dict[str, list[str]]

# --------------------------------------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------------------------------------


def _inside(root: Path, rel: str) -> Path | None:
    """Return ``root / rel`` when ``rel`` is a relative path that stays inside ``root``; otherwise None."""
    text = rel.replace("\\", "/").strip()
    if not text or PurePosixPath(text).is_absolute() or PureWindowsPath(rel).is_absolute():
        return None
    if ".." in PurePosixPath(text).parts:
        return None
    path = root / text
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return path


def _candidate_rank(path: Path) -> tuple[int, int, int, str] | None:
    """Rank one directory entry as a changelog candidate, or return None when it is not one."""
    if path.is_symlink() or not path.is_file():
        return None
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix not in _EXTENSIONS:
        return None
    stem = name[: len(name) - len(suffix)] if suffix else name
    for stem_rank, wanted in enumerate(_STEMS):
        exact = stem == wanted
        if exact or (stem.startswith(wanted) and stem[len(wanted)] in "-_. "):
            return stem_rank, 0 if exact else 1, list(_EXTENSIONS).index(suffix), path.name
    return None


def _find_changelog(root: Path) -> Path | None:
    """Return the highest-precedence changelog file under the searched directories, or None."""
    for rel in _SEARCH_DIRS:
        folder = root / rel if rel else root
        if folder.is_symlink() or not folder.is_dir():
            continue
        ranked = sorted((rank, entry) for entry in folder.iterdir() if (rank := _candidate_rank(entry)) is not None)
        if ranked:
            return ranked[0][1]
    return None


def _towncrier_settings(text: str) -> dict[str, str] | None:
    """Return ``directory``/``filename`` values of a ``[tool.towncrier]`` TOML section, or None when it is absent.

    A regex reader, because ``tomllib`` needs Python 3.11 and the floor is 3.10; only plain string values are read.
    """
    settings: dict[str, str] | None = None
    inside = False
    for line in text.splitlines():
        if _TOML_HEADER.match(line):
            inside = bool(_TOWNCRIER_HEADER.match(line))
            settings = {} if inside and settings is None else settings
            continue
        match = _TOML_STRING.match(line) if inside else None
        if match is not None and settings is not None:
            settings.setdefault(match.group("key"), match.group("value"))
    return settings


def _fragment_files(root: Path, directory: str | None) -> tuple[str | None, list[str]]:
    """Return the fragment directory used and its fragment files (repo-relative POSIX paths)."""
    names = [directory] if directory else list(_FRAGMENT_DIRS)
    for name in names:
        folder = _inside(root, name)
        if folder is None or folder.is_symlink() or not folder.is_dir():
            continue
        files = sorted(
            entry.relative_to(root).as_posix()
            for entry in folder.iterdir()
            if entry.is_file() and not entry.is_symlink() and _FRAGMENT_NAME.match(entry.name)
        )
        return folder.relative_to(root).as_posix(), files
    return None, []


def _find_towncrier(root: Path) -> dict[str, Any] | None:
    """Return the towncrier setup (config file, fragment directory, rendered filename, fragments), or None."""
    for config in ("towncrier.toml", "pyproject.toml"):
        path = root / config
        if path.is_symlink() or not path.is_file():
            continue
        settings = _towncrier_settings(read_bounded_text(path, errors="replace"))
        if settings is None and config == "pyproject.toml":
            continue
        settings = settings or {}
        directory, fragments = _fragment_files(root, settings.get("directory"))
        return {
            "config": config,
            "directory": directory,
            "filename": settings.get("filename"),
            "fragments": fragments,
        }
    return None


def discover_changelog(repo: Path | str) -> dict[str, Any]:
    """Locate the repository's changelog file and towncrier setup.

    Args:
        repo: Repository root (working tree).

    Returns:
        ``{"path": str | None, "format": "md" | "rst" | "txt" | None, "towncrier": dict | None}``; ``path`` is
        repo-relative POSIX. When no candidate exists but towncrier names an existing ``filename``, that file is used.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     discover_changelog(tmp)
        {'path': None, 'format': None, 'towncrier': None}
    """
    root = Path(repo)
    towncrier = _find_towncrier(root)
    found = _find_changelog(root)
    if found is None and towncrier is not None and towncrier["filename"]:
        named = _inside(root, towncrier["filename"])
        found = named if named is not None and named.is_file() and not named.is_symlink() else None
    if found is None:
        return {"path": None, "format": None, "towncrier": towncrier}
    return {
        "path": found.relative_to(root).as_posix(),
        "format": _EXTENSIONS.get(found.suffix.lower(), "txt"),
        "towncrier": towncrier,
    }


# --------------------------------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------------------------------


def _is_title_line(line: str) -> bool:
    """Return True when a line can be the text of an underlined heading."""
    stripped = line.strip()
    if not stripped or line[:1].isspace() or _BULLET.match(line) or _ADORNMENT.match(line):
        return False
    return not _ATX.match(line) and not _FENCE.match(line)


def _md_events(lines: list[str]) -> list[tuple[str, int, str]]:
    """Split Markdown lines into ``("heading", level, text)`` and ``("line", 0, text)`` events; fenced code is
    skipped."""
    events: list[tuple[str, int, str]] = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if _FENCE.match(line):
            in_fence = not in_fence
        elif in_fence:
            pass
        elif (atx := _ATX.match(line)) is not None:
            events.append(("heading", len(atx.group("hashes")), re.sub(r"\s+#+$", "", atx.group("text").rstrip())))
        elif _SETEXT.match(following) and _is_title_line(line):
            events.append(("heading", 1 if following.strip()[0] == "=" else 2, line.strip()))
            index += 1
        else:
            events.append(("line", 0, line))
        index += 1
    return events


def _rst_events(lines: list[str]) -> list[tuple[str, int, str]]:
    """Split reStructuredText lines into heading and line events; levels follow adornment order of first use."""
    events: list[tuple[str, int, str]] = []
    styles: dict[tuple[str, bool], int] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        following = lines[index + 1] if index + 1 < len(lines) else ""
        third = lines[index + 2] if index + 2 < len(lines) else ""
        if _ADORNMENT.match(line) and _is_title_line(following.strip()) and third.strip() == line.strip():
            level = styles.setdefault((line.strip()[0], True), len(styles) + 1)
            events.append(("heading", level, following.strip()))
            index += 3
        elif _is_title_line(line) and _ADORNMENT.match(following):
            level = styles.setdefault((following.strip()[0], False), len(styles) + 1)
            events.append(("heading", level, line.strip()))
            index += 2
        else:
            events.append(("line", 0, line))
            index += 1
    return events


def _clean_heading(text: str) -> str:
    """Strip link targets and inline markup from heading text, keeping the visible words."""
    out = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    out = re.sub(r"<[^>]*>", " ", out)
    out = re.sub(r"[`*\[\]]", " ", out)
    return " ".join(out.split())


def _heading_version(clean: str) -> str | None:
    """Return the canonical version (or ``unreleased``) a heading names, or None."""
    for match in _VERSION_TOKEN.finditer(clean):
        version = parse_version(match.group(0))
        if version is not None:
            return version.canonical
    return UNRELEASED if _UNRELEASED_WORD.search(clean) else None


def _subsection_bucket(text: str) -> str | None:
    """Map a subsection title (Added, Bug Fixes, Deprecations and Removals, ...) to its bucket, or None."""
    words = " ".join(re.findall(r"[a-z]+", text.lower()))
    if not words or len(words.split()) > 4 or "contributor" in words:
        return None
    for bucket, pattern in _SUBSECTION_RULES:
        if pattern.search(words):
            return bucket
    return None


def _flat_bucket(text: str) -> str | None:
    """Return the bucket a bullet outside any subsection belongs to, judged by its leading words, or None."""
    for bucket, pattern in _FLAT_RULES:
        if pattern.search(text):
            return bucket
    return None


@dataclass
class _SectionParser:
    """Line-event state machine collecting bullets per version and bucket.

    Version headings are recognised at or above the level of the first version heading; deeper headings are subsections.
    Bullets under an unrecognised subsection (Documentation, Internal, ...) are dropped. Bullets directly under a
    version heading, with no subsection at all, are classified by their leading words (:func:`_flat_bucket`) and counted
    in ``flat_kept`` or ``flat_left``. Indented lines, nested bullets and lazy continuation lines join the open bullet.
    """

    out: dict[str, Buckets] = field(default_factory=dict)
    version: str | None = None
    version_level: int | None = None
    bucket: str | None = None
    subsection: bool = False
    parts: list[str] | None = None
    top_indent: int | None = None
    after_blank: bool = False
    flat_kept: int = 0
    flat_left: int = 0

    def flush(self) -> None:
        """Close the open bullet and store it under the current version and bucket."""
        if self.parts and self.version is not None:
            text = " ".join(" ".join(self.parts).split())
            bucket = self.bucket
            if bucket is None and not self.subsection:
                bucket = _flat_bucket(text)
                self.flat_kept += bucket is not None
                self.flat_left += bucket is None
            if bucket is not None:
                self.out[self.version][bucket].append(text)
        self.parts = None

    def heading(self, level: int, text: str) -> None:
        """Handle one heading event."""
        self.flush()
        self.top_indent = None
        clean = _clean_heading(text)
        at_version_depth = self.version_level is None or level <= self.version_level
        version = _heading_version(clean) if at_version_depth else None
        if version is not None:
            self.version, self.bucket, self.subsection = version, None, False
            self.version_level = level if self.version_level is None else self.version_level
            self.out.setdefault(version, {bucket: [] for bucket in _BUCKETS})
        elif self.version_level is not None and level <= self.version_level:
            self.version, self.bucket = None, None
        elif _WHATS_CHANGED.fullmatch(clean):
            self.bucket, self.subsection = None, False
        else:
            self.bucket, self.subsection = _subsection_bucket(clean), True

    def line(self, text: str) -> None:
        """Handle one non-heading line."""
        if self.version is None:
            return
        if not text.strip():
            self.after_blank = True
            return
        indent = len(text) - len(text.lstrip())
        pseudo = _PSEUDO_HEADING.match(text) if indent == 0 else None
        if pseudo is not None:
            self.flush()
            self.bucket, self.top_indent = _subsection_bucket(pseudo.group("a") or pseudo.group("b")), None
            self.subsection = True
        else:
            self._body_line(text, indent)
        self.after_blank = False

    def _body_line(self, text: str, indent: int) -> None:
        """Open a bullet, continue the open one (indented, nested or lazy line), or close it on unrelated text."""
        bullet = _BULLET.match(text)
        if bullet is not None and (self.top_indent is None or indent <= self.top_indent):
            self.flush()
            self.parts = [bullet.group("text")]
            self.top_indent = indent if self.top_indent is None else self.top_indent
        elif self.parts is not None and (indent > (self.top_indent or 0) or not self.after_blank):
            self.parts.append(bullet.group("text") if bullet is not None else text.strip())
        else:
            self.flush()

    def feed(self, events: list[tuple[str, int, str]]) -> dict[str, Buckets]:
        """Consume all events and return the collected sections."""
        for kind, level, text in events:
            if kind == "heading":
                self.heading(level, text)
            else:
                self.line(text)
        self.flush()
        return self.out


def parse_changelog(text: str, fmt: str, *, warnings: list[str] | None = None) -> dict[str, Buckets]:
    """Split changelog text into per-version Added/Changed/Fixed bullet lists.

    Version headings: Markdown ``## [1.2.0] - date``, ``## 1.2.0``, ``## v1.2.0 (date)``, ``## [Unreleased]``
    (key ``unreleased``), setext and reStructuredText underlined titles. Subsection synonyms: Added/New/Features to
    ``added``; Changed/Improved/Changes/Deprecated/Removed to ``changed``; Fixed/Fixes/Bug Fixes/Security to ``fixed``.
    Bold or colon-terminated lines (``**Fixed**``, ``Fixed:``) also open a subsection. Multi-line and nested bullets
    are joined into one entry. Keys are canonical versions (:func:`parse_version`) in file order; a version listed
    twice has its bullets concatenated. ``txt`` tries Markdown first and falls back to reStructuredText when that
    finds no version.

    Args:
        text: Changelog text (untrusted; parsed as data only).
        fmt: ``"md"``, ``"rst"`` or ``"txt"``.
        warnings: Optional list receiving a warning when lines are truncated to 10000 characters.

    Returns:
        ``{version: {"added": [...], "changed": [...], "fixed": [...]}}``.

    Raises:
        ValueError: ``fmt`` is unsupported.

    Examples:
        >>> parse_changelog("## [1.2.0] - 2024-01-01\\n### Fixed\\n- Crash on start\\n  when empty\\n", "md")
        {'1.2.0': {'added': [], 'changed': [], 'fixed': ['Crash on start when empty']}}
    """
    lines = [line.expandtabs(4) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    truncated = sum(len(line) > MAX_ENTRY_LINE_CHARS for line in lines)
    if truncated and warnings is not None:
        warnings.append(f"changelog: truncated {truncated} line(s) to {MAX_ENTRY_LINE_CHARS} characters")
    lines = [line[:MAX_ENTRY_LINE_CHARS] for line in lines]
    if fmt not in ("md", "rst", "txt"):
        raise ValueError(f"unsupported changelog format {fmt!r}; expected md, rst or txt")
    parser = _SectionParser()
    parsed = parser.feed(_md_events(lines) if fmt in ("md", "txt") else _rst_events(lines))
    if fmt == "txt" and not parsed:
        parser = _SectionParser()
        parsed = parser.feed(_rst_events(lines))
    if parser.flat_kept + parser.flat_left and warnings is not None:
        warnings.append(
            f"changelog: {parser.flat_kept + parser.flat_left} bullet(s) outside Added/Changed/Fixed sections; "
            f"{parser.flat_kept} classified by leading words, {parser.flat_left} left out"
        )
    return parsed


# --------------------------------------------------------------------------------------------------------------------
# GitHub (optional)
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GhItem:
    """One GitHub issue or pull request as the linkage ladder sees it.

    Attributes:
        kind: ``"pr"`` or ``"issue"``.
        merge_commit: Merge (or squash) commit of a merged pull request; None otherwise.
        commits: The pull request's own commit SHAs.
        linked: Closing issue numbers of a pull request, or closing pull request numbers of an issue.
        linked_commits: Merge commits of an issue's merged closing pull requests.
        labels: Label names as GitHub returns them.
    """

    kind: Literal["pr", "issue"]
    merge_commit: str | None = None
    commits: tuple[str, ...] = ()
    linked: tuple[int, ...] = ()
    linked_commits: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()


class GitHubUnavailable(Exception):
    """A GitHub lookup failed; linkage continues from local commit messages only."""


class GitHubLookup(Protocol):
    """Lookups the ladder needs from GitHub; :class:`_GhCli` is the ``gh`` implementation, tests pass a fake."""

    def items(self, numbers: list[int]) -> dict[int, GhItem]:
        """Return the issue or pull request behind each number (unknown numbers are absent)."""
        ...

    def commit_prs(self, shas: list[str]) -> dict[str, list[int]]:
        """Return the merged pull request numbers associated with each commit."""
        ...


def _nodes(connection: Any) -> list[dict[str, Any]]:
    """Return the dict nodes of a GraphQL connection, tolerating nulls."""
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    return [node for node in nodes or [] if isinstance(node, dict)]


def _oid(node: Any) -> str | None:
    """Return a full commit SHA from a ``{"oid": ...}`` node, or None."""
    oid = node.get("oid") if isinstance(node, dict) else None
    return oid if isinstance(oid, str) and _SHA_FULL.match(oid) else None


def _numbers(nodes: list[dict[str, Any]]) -> tuple[int, ...]:
    """Return the integer ``number`` fields of GraphQL nodes."""
    return tuple(number for node in nodes if isinstance(number := node.get("number"), int))


def _parse_pr(node: dict[str, Any], labels: tuple[str, ...]) -> GhItem:
    """Convert a ``PullRequest`` node: merge commit, own commits, closing issues."""
    commits = tuple(oid for item in _nodes(node.get("commits")) if (oid := _oid(item.get("commit"))))
    closing = _numbers(_nodes(node.get("closingIssuesReferences")))
    return GhItem("pr", _oid(node.get("mergeCommit")), commits, closing, (), labels)


def _parse_issue(node: dict[str, Any], labels: tuple[str, ...]) -> GhItem:
    """Convert an ``Issue`` node: closing pull requests and their merge commits."""
    prs = _nodes(node.get("closedByPullRequestsReferences"))
    merges = tuple(oid for item in prs if (oid := _oid(item.get("mergeCommit"))))
    return GhItem("issue", None, (), _numbers(prs), merges, labels)


#: GraphQL ``__typename`` to node converter.
_ITEM_PARSERS: Final = {"PullRequest": _parse_pr, "Issue": _parse_issue}


def _parse_item(node: Any) -> GhItem | None:
    """Convert one ``issueOrPullRequest`` GraphQL node into a :class:`GhItem` (None for null or unknown nodes)."""
    parser = _ITEM_PARSERS.get(node.get("__typename")) if isinstance(node, dict) else None
    if parser is None:
        return None
    labels = tuple(sorted(str(label.get("name", "")) for label in _nodes(node.get("labels"))))
    return parser(node, labels)


def _items_query(numbers: list[int]) -> str:
    """Return one GraphQL document resolving every number to its issue or pull request."""
    pr_fields = (
        "... on PullRequest { mergeCommit { oid } labels(first: 20) { nodes { name } } "
        "closingIssuesReferences(first: 20) { nodes { number } } commits(first: 100) { nodes { commit { oid } } } }"
    )
    issue_fields = (
        "... on Issue { labels(first: 20) { nodes { name } } "
        "closedByPullRequestsReferences(first: 10, includeClosedPrs: true) { nodes { number mergeCommit { oid } } } }"
    )
    aliases = " ".join(
        f"n{int(number)}: issueOrPullRequest(number: {int(number)}) {{ __typename {pr_fields} {issue_fields} }}"
        for number in numbers
    )
    return f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {aliases} }} }}"


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    """Split ``items`` into consecutive chunks of at most ``size``."""
    return [items[start : start + size] for start in range(0, len(items), size)]


class _GhCli:
    """GitHub lookups through ``gh api graphql``, reusing ``gh_batch_query``'s request and commit-PR helpers."""

    def __init__(self, root: Path) -> None:
        """Bind the client to a repository checkout (``gh`` resolves OWNER/NAME from it)."""
        self.root = root
        self._slug: tuple[str, str] | None = None

    def _call(self, build: Any) -> Any:
        """Run one lookup, converting every ``gh`` failure into :class:`GitHubUnavailable`."""
        import gh_batch_query as ghq

        for attempt in range(1, _GH_ATTEMPTS + 1):
            try:
                if self._slug is None:
                    self._slug = ghq.resolve_repo(None, self.root)
                return build(ghq, *self._slug)
            except ghq.QueryError as exc:
                # A timeout or dropped connection often clears on its own; an error the API returned never does.
                if attempt == _GH_ATTEMPTS or not str(exc).startswith(_GH_NETWORK_FAILURE):
                    raise GitHubUnavailable(str(exc)) from exc
                time.sleep(_GH_BACKOFF_SECONDS * attempt)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                raise GitHubUnavailable(str(exc)) from exc
        raise GitHubUnavailable("no attempt was made")

    def items(self, numbers: list[int]) -> dict[int, GhItem]:
        """Resolve numbers to issues or pull requests in batched GraphQL requests."""

        def build(ghq: Any, owner: str, name: str) -> dict[int, GhItem]:
            """Send the batches and parse every alias."""
            found: dict[int, GhItem] = {}
            for batch in _chunks(numbers, _GH_BATCH):
                data = ghq.run_graphql(_items_query(batch), owner, name, self.root)
                found.update({n: item for n in batch if (item := _parse_item(data.get(f"n{n}"))) is not None})
            return found

        return self._call(build)

    def release_bodies(self) -> dict[str, str]:
        """Return the notes of every GitHub Release by tag name, newest first, paging until the list ends."""

        def build(ghq: Any, owner: str, name: str) -> dict[str, str]:
            """Page through the repository's releases and collect tag name to description."""
            found: dict[str, str] = {}
            cursor = "null"
            for _ in range(_GH_RELEASE_PAGES):
                query = (
                    "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { "
                    f"releases(first: 100, after: {cursor}, orderBy: {{field: CREATED_AT, direction: DESC}}) "
                    "{ nodes { tagName description } pageInfo { hasNextPage endCursor } } } }"
                )
                data = ghq.run_graphql(query, owner, name, self.root).get("releases") or {}
                for node in _nodes(data):
                    if isinstance(node.get("tagName"), str) and isinstance(node.get("description"), str):
                        found[node["tagName"]] = node["description"]
                page = data.get("pageInfo") or {}
                if not page.get("hasNextPage"):
                    return found
                cursor = json.dumps(str(page.get("endCursor")))
            raise GitHubUnavailable(f"more than {_GH_RELEASE_PAGES * 100} GitHub Releases")

        return self._call(build)

    def commit_prs(self, shas: list[str]) -> dict[str, list[int]]:
        """Map commits to their merged pull requests via ``gh_batch_query``'s batched query."""

        def build(ghq: Any, owner: str, name: str) -> dict[str, list[int]]:
            """Send the batches and collect PR numbers per commit."""
            found: dict[str, list[int]] = {}
            for batch in _chunks(shas, _GH_BATCH):
                data = ghq.run_graphql(ghq.build_commit_query(batch), owner, name, self.root)
                for record in ghq.parse_commit_response(data, batch):
                    found[record["commit"]] = [int(pr["number"]) for pr in record.get("pull_requests", [])]
            return found

        return self._call(build)


class _GhSession:
    """Cached, failure-tolerant wrapper: the first failure records a warning and disables GitHub for the run."""

    def __init__(self, client: GitHubLookup | None, warnings: list[str]) -> None:
        """Wrap ``client`` (None = GitHub unavailable)."""
        self.client = client
        self.warnings = warnings
        self._items: dict[int, GhItem | None] = {}

    @property
    def active(self) -> bool:
        """Return True while GitHub lookups are available."""
        return self.client is not None

    def _fail(self, exc: GitHubUnavailable) -> None:
        """Record the failure once and stop using GitHub."""
        self.warnings.append(f"gh lookup failed ({exc}); continuing with local commit messages only")
        self.client = None

    def items(self, numbers: Iterable[int]) -> dict[int, GhItem]:
        """Return cached-or-fetched items for ``numbers``; empty when GitHub is unavailable."""
        wanted = sorted(set(numbers))
        missing = [number for number in wanted if number not in self._items]
        if missing and self.client is not None:
            try:
                found = self.client.items(missing)
            except GitHubUnavailable as exc:
                self._fail(exc)
                return {}
            self._items.update({number: found.get(number) for number in missing})
        return {number: item for number in wanted if (item := self._items.get(number)) is not None}

    def release_bodies(self) -> dict[str, str]:
        """Return GitHub Release notes by tag; empty when GitHub is unavailable or the client cannot list releases."""
        lookup = getattr(self.client, "release_bodies", None)
        if lookup is None:
            return {}
        try:
            return dict(lookup())
        except GitHubUnavailable as exc:
            self._fail(exc)
            return {}

    def commit_prs(self, shas: list[str]) -> dict[str, list[int]]:
        """Return pull request numbers per commit; empty when GitHub is unavailable."""
        if not shas or self.client is None:
            return {}
        try:
            return self.client.commit_prs(shas)
        except GitHubUnavailable as exc:
            self._fail(exc)
            return {}


# --------------------------------------------------------------------------------------------------------------------
# Git history per release
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Commit:
    """One commit of a release range.

    Attributes:
        sha: Full commit SHA.
        parents: Parent SHAs, first parent first.
        title: Subject, or the pull request title (first body line) of a GitHub merge commit.
        pr: Pull request number read locally from a ``(#N)`` subject suffix or ``Merge pull request #N``.
        reapplies: Pull request number of the original change when the subject is ``Reapply "<subject> (#N)"``: the
            commit re-adds that pull request's lines after a revert.
    """

    sha: str
    parents: tuple[str, ...]
    title: str
    pr: int | None
    reapplies: int | None = None


@dataclass(eq=False)
class _Release:
    """One release (or the ``unreleased`` pseudo-release) with its commit range."""

    version: str
    order: int
    line: str
    commits: list[_Commit]
    first_parent: list[str]
    tag: str = ""


def _make_commit(sha: str, parents: list[str], subject: str, body: str) -> _Commit:
    """Build a :class:`_Commit`, reading the local PR number and PR title from the message."""
    merge = _MERGE_PR.match(subject)
    if merge is not None:
        title = next((line.strip() for line in body.splitlines() if line.strip()), subject)
        return _Commit(sha, tuple(parents), title, int(merge.group("n")))
    squash = _SQUASH_PR.search(subject)
    reapply = _REAPPLY.match(subject)
    original = _SQUASH_PR.search(reapply.group("inner")) if reapply else None
    return _Commit(
        sha,
        tuple(parents),
        subject,
        int(squash.group("n")) if squash else None,
        int(original.group("n")) if original else None,
    )


def _parse_log(text: str) -> list[_Commit]:
    """Parse ``git log --format=%H%x00%P%x00%s%x00%b%x1e`` output; malformed records are skipped."""
    commits: list[_Commit] = []
    for record in text.split("\x1e"):
        fields = record.strip("\n").split("\x00", 3)
        if len(fields) == 4 and _SHA_FULL.match(fields[0]):
            commits.append(_make_commit(fields[0], fields[1].split(), fields[2], fields[3]))
    return commits


def _checked_sha(value: Any) -> str:
    """Return ``value`` when it is a full hex SHA (so it can never be read as a git option); raise otherwise."""
    if not isinstance(value, str) or not _SHA_FULL.match(value):
        raise ValueError(f"index holds an invalid commit SHA: {value!r}")
    return value


def _first_parent_chain(tip: str | None, commits: list[_Commit]) -> list[str]:
    """Return the first-parent chain from ``tip`` while it stays inside ``commits`` (newest first)."""
    by_sha = {commit.sha: commit for commit in commits}
    chain: list[str] = []
    current = tip
    while current is not None and current in by_sha and current not in chain:
        chain.append(current)
        parents = by_sha[current].parents
        current = parents[0] if parents else None
    return chain


def _read_commits(root: Path, shas: Iterable[str]) -> dict[str, _Commit]:
    """Read parents and message of exactly the given commits in one ``git log --no-walk --stdin`` call."""
    wanted = sorted(set(shas))
    if not wanted:
        return {}
    stdin = "".join(f"{sha}\n" for sha in wanted)
    args = ("log", "--no-walk=unsorted", "--stdin", f"--format={_LOG_FORMAT}")
    return {commit.sha: commit for commit in _parse_log(run_git(root, *args, stdin=stdin, timeout=_LOG_TIMEOUT_S))}


def _shipped_by(index: dict[str, Any]) -> dict[tuple[str, str], list[str]]:
    """Group ``index["commit_releases"]`` by ``(line, version)``: the commits each release first ships on each line."""
    grouped: dict[tuple[str, str], list[str]] = {}
    for sha, per_line in sorted((index.get("commit_releases") or {}).items()):
        for line, version in sorted(per_line.items()):
            grouped.setdefault((str(line), str(version)), []).append(_checked_sha(sha))
    return grouped


def _unreleased_line(chain: list[str], commits: dict[str, _Commit], releases: list[_Release]) -> str:
    """Return the line of the release the unreleased first-parent chain grows from (newest release's line else)."""
    parents = commits[chain[-1]].parents if chain else ()
    boundary = parents[0] if parents else None
    line_of = {commit.sha: release.line for release in reversed(releases) for commit in release.commits}
    if boundary is not None and boundary in line_of:
        return line_of[boundary]
    return releases[-1].line if releases else ""


def _build_releases(root: Path, index: dict[str, Any]) -> dict[str, _Release]:
    """Return every indexed release plus ``unreleased``, each with its commit range and first-parent chain.

    A release's range is the commits ``index["commit_releases"]`` credits to it on its own line (``release["line"]``),
    i.e. the commits it first ships along that line's chain. ``index["predecessor"]`` is deliberately not used: it
    follows ancestry only and can skip an off-branch release of the same line, so ``<tag> ^<predecessor>`` may repeat
    commits an earlier release already shipped. The ``unreleased`` range is exactly ``index["head"]["unreleased"]``.
    """
    records = sorted(index["releases"], key=lambda record: record["order"])
    shipped = _shipped_by(index)
    head = index.get("head") or {}
    pending = [_checked_sha(sha) for sha in head.get("unreleased") or []]
    data = _read_commits(root, [*(sha for group in shipped.values() for sha in group), *pending])
    releases: list[_Release] = []
    for record in records:
        version, line, tip = record["version"], str(record["line"]), _checked_sha(record["sha"])
        commits = [data[sha] for sha in shipped.get((line, version), []) if sha in data]
        chain = _first_parent_chain(tip, commits)
        releases.append(_Release(version, int(record["order"]), line, commits, chain, str(record.get("tag") or "")))
    out = {release.version: release for release in releases}
    out[UNRELEASED] = _unreleased_release(
        head.get("sha"), [data[sha] for sha in pending if sha in data], data, releases
    )
    return out


def _unreleased_release(
    head_sha: str | None, commits: list[_Commit], data: dict[str, _Commit], releases: list[_Release]
) -> _Release:
    """Return the ``unreleased`` pseudo-release, ordered after every release, on the line its commits grow from."""
    chain = _first_parent_chain(head_sha, commits)
    order = releases[-1].order + 1 if releases else 0
    return _Release(UNRELEASED, order, _unreleased_line(chain, data, releases), commits, chain)


class _GitObjects:
    """Cached per-commit git facts: patch-ids, footprint files and commit existence."""

    def __init__(self, root: Path, releases: dict[str, _Release]) -> None:
        """Seed the parent cache from the release ranges already read."""
        self.root = root
        self.parents = {commit.sha: commit.parents for release in releases.values() for commit in release.commits}
        self._patch: dict[str, str | None] = {}
        self._empty_tree: str | None = None

    def _base(self, sha: str) -> str:
        """Return the diff base of ``sha``: its first parent, or the empty tree for a root commit."""
        parents = self.parents.get(sha)
        base = (parents[0] if parents else None) if parents is not None else first_parent(self.root, sha)
        if base is not None:
            return base
        if self._empty_tree is None:
            self._empty_tree = run_git(self.root, "hash-object", "-t", "tree", "--stdin", stdin="").strip()
        return self._empty_tree

    def patch_id(self, sha: str) -> str | None:
        """Return the stable patch-id of the first-parent diff of ``sha`` (None for an empty diff)."""
        if sha not in self._patch:
            diff = run_git(self.root, "diff-tree", "-p", "-r", "--no-color", self._base(sha), sha)
            out = run_git(self.root, "patch-id", "--stable", stdin=diff).split() if diff.strip() else []
            self._patch[sha] = out[0] if out else None
        return self._patch[sha]

    def patch_ids(self, shas: Iterable[str]) -> list[str]:
        """Return the sorted, de-duplicated patch-ids of several commits."""
        return sorted({pid for sha in shas if (pid := self.patch_id(sha)) is not None})

    def changed_files(self, sha: str) -> list[str]:
        """Return the files ``sha`` adds or modifies relative to its first parent (deletions excluded)."""
        out = run_git(self.root, "diff-tree", "-r", "-z", "--no-renames", "--name-status", self._base(sha), sha)
        fields = [part for part in out.split("\x00") if part]
        pairs = zip(fields[0::2], fields[1::2])
        return sorted({path for status, path in pairs if status[:1] in ("A", "M", "T")})

    def merged_commits(self, sha: str) -> list[str]:
        """Return the commits a merge commit brought in from its side branch; empty for any other commit.

        A pull request merged with a merge commit keeps its own branch commits, and blame points at those, never at the
        merge. A side branch longer than :data:`_MERGED_LIMIT` is a branch merge, not one pull request, and is ignored.
        """
        parents = self.parents.get(sha)
        if parents is None or len(parents) < 2:
            return []
        out = run_git(self.root, "rev-list", f"--max-count={_MERGED_LIMIT + 2}", f"{parents[0]}..{sha}").split()
        side = [line for line in out if line != sha and _SHA_FULL.match(line)]
        return side if len(side) <= _MERGED_LIMIT else []

    def existing_commits(self, tokens: Iterable[str]) -> dict[str, str]:
        """Resolve SHA-like tokens to full commit SHAs in one ``cat-file`` call; non-commits are absent."""
        wanted = sorted(set(tokens))
        if not wanted:
            return {}
        stdin = "".join(f"{token}^{{commit}}\n" for token in wanted)
        out = run_git(self.root, "cat-file", "--batch-check=%(objectname) %(objecttype)", stdin=stdin)
        resolved: dict[str, str] = {}
        for token, line in zip(wanted, out.splitlines()):
            parts = line.split()
            if len(parts) == 2 and parts[1] == "commit" and _SHA_FULL.match(parts[0]):
                resolved[token] = parts[0]
        return resolved


# --------------------------------------------------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class _Refs:
    """References found in an entry's text."""

    shas: list[str] = field(default_factory=list)
    prs: list[int] = field(default_factory=list)
    issues: list[int] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)


@dataclass
class _Draft:
    """An entry being built; serialised by :func:`_record` at the end."""

    entry_id: str
    kind: str
    release: str
    line: str
    text: str
    source: str
    position: int
    refs: _Refs
    sha_tokens: list[str] = field(default_factory=list)
    version_tokens: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    link_method: str = "unlinked"
    link_confidence: float = 0.0
    patch_ids: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    secondary_releases: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    text_refs: list[str] = field(default_factory=list)
    closing: set[int] = field(default_factory=set)


def _origin_slug(root: Path) -> str | None:
    """Return ``owner/name`` (lower case) of the ``origin`` remote, or None when absent or unparsable."""
    url = run_git(root, "remote", "get-url", "origin", check=False).strip()
    parts = parse_remote_url(url) if url else None
    return "/".join(parts[-2:]).lower() if parts else None


def _parse_refs(text: str, slug: str | None) -> tuple[_Refs, list[str], list[str]]:
    """Return the PR/issue references, SHA-like tokens and regression-version tokens found in ``text``.

    A bare ``#N`` (or ``GH-N``) is recorded as a PR number until GitHub says it is an issue. URLs count only when they
    point at this repository (or the repository has no parsable ``origin``). SHA tokens need a digit and a letter, so
    plain numbers and hex-looking words are ignored.
    """
    prs = {int(n) for n in _HASH_REF.findall(text)} | {int(n) for n in _GH_REF.findall(text)}
    issues: set[int] = set()
    for match in _URL_REF.finditer(text):
        if slug is None or match.group("slug").lower() == slug:
            (prs if match.group("kind") == "pull" else issues).add(int(match.group("num")))
    shas = sorted(
        {token for token in _SHA_TOKEN.findall(text) if re.search(r"\d", token) and re.search(r"[a-f]", token)}
    )
    versions = sorted({match.group("v").rstrip(".,;:)") for match in _REGRESSION.finditer(text)})
    return _Refs(prs=sorted(prs - issues), issues=sorted(issues)), shas, versions


def _new_draft(release: _Release, kind: str, text: str, source: str, position: int, slug: str | None) -> _Draft:
    """Create a draft entry with its parsed references (ID assigned by the caller)."""
    refs, shas, versions = _parse_refs(text, slug)
    return _Draft("", kind, release.version, release.line, text, source, position, refs, shas, versions)


def _changelog_drafts(release: _Release, buckets: Buckets, slug: str | None) -> list[_Draft]:
    """Return one draft per changelog bullet; columns (added + changed) share one ID namespace."""
    fixes = [_new_draft(release, "fix", text, SOURCE_CHANGELOG, pos, slug) for pos, text in enumerate(buckets["fixed"])]
    column_texts = [("feature", text) for text in buckets["added"]] + [("change", text) for text in buckets["changed"]]
    columns = [
        _new_draft(release, kind, text, SOURCE_CHANGELOG, pos, slug) for pos, (kind, text) in enumerate(column_texts)
    ]
    for group in (fixes, columns):
        for draft, entry_id in zip(group, entry_ids(release.version, [draft.text for draft in group])):
            draft.entry_id = entry_id
    return fixes + columns


def _conventional_kind(title: str) -> str | None:
    """Return ``fix``/``feature`` for a conventional-commit title, else None."""
    match = _CONVENTIONAL.match(title)
    return _CONVENTIONAL_KIND.get(match.group("type").lower()) if match else None


def _label_kind(labels: Iterable[str]) -> str | None:
    """Return ``fix``/``feature`` from PR labels (fix labels win, then refactor labels), else None."""
    lowered = {label.lower() for label in labels}
    if lowered & _FIX_LABELS:
        return "fix"
    if lowered & _REFACTOR_LABELS:
        return None
    return "feature" if lowered & _FEATURE_LABELS else None


def _heuristic_drafts(
    release: _Release, label_kinds: dict[str, str], objects: _GitObjects, slug: str | None
) -> list[_Draft]:
    """Classify the release's first-parent commits by conventional prefix, then PR label; one draft per hit."""
    by_sha = {commit.sha: commit for commit in release.commits}
    drafts: list[_Draft] = []
    for position, sha in enumerate(reversed(release.first_parent)):
        commit = by_sha[sha]
        labelled = None if _REFACTOR_TITLE.search(commit.title) else label_kinds.get(sha)
        kind = _conventional_kind(commit.title) or labelled
        patch = objects.patch_id(sha) if kind else None
        if kind is None or patch is None:
            continue
        draft = _new_draft(release, kind, commit.title, SOURCE_HEURISTIC, position, slug)
        draft.entry_id, draft.commits, draft.patch_ids = commit_id(patch), [sha], [patch]
        draft.link_method, draft.link_confidence = "heuristic", _CONFIDENCE_HEURISTIC
        drafts.append(draft)
    return drafts


def _unprefixed_commits(releases: list[_Release]) -> list[_Commit]:
    """Return the first-parent commits of ``releases`` whose title has no conventional fix/feat prefix."""
    by_sha = {commit.sha: commit for release in releases for commit in release.commits}
    chains = [by_sha[sha] for release in releases for sha in release.first_parent]
    return [commit for commit in chains if _conventional_kind(commit.title) is None]


def _pr_numbers(commits: list[_Commit], gh: _GhSession) -> dict[str, list[int]]:
    """Return PR numbers per commit: the local ``(#N)``/merge number, else GitHub's associated PRs."""
    via_gh = gh.commit_prs([commit.sha for commit in commits if commit.pr is None])
    return {commit.sha: [commit.pr] if commit.pr is not None else via_gh.get(commit.sha, []) for commit in commits}


def _label_kinds(releases: list[_Release], gh: _GhSession) -> dict[str, str]:
    """Return ``{sha: kind}`` from PR labels for unprefixed first-parent commits of heuristic releases (gh only)."""
    if not gh.active:
        return {}
    numbers = _pr_numbers(_unprefixed_commits(releases), gh)
    items = gh.items(n for found in numbers.values() for n in found)
    kinds: dict[str, str] = {}
    for sha, found in numbers.items():
        kind = _label_kind(label for n in found if n in items for label in items[n].labels)
        if kind is not None:
            kinds[sha] = kind
    return kinds


# --------------------------------------------------------------------------------------------------------------------
# Linkage ladder
# --------------------------------------------------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    """Return the comparable words of a text: normalised, conventional prefix dropped, short and stop words removed."""
    match = _CONVENTIONAL.match(text.strip())
    body = match.group("rest") if match and _CONVENTIONAL_KIND.get(match.group("type").lower()) else text
    words = re.findall(r"[a-z0-9_]+", normalise_entry_text(body))
    return {word for word in words if len(word) >= 3 and word not in _STOPWORDS}


def _text_match(text: str, commits: list[_Commit]) -> tuple[list[str], float] | None:
    """Return the best-matching commits by token overlap and the capped confidence, or None below the bar."""
    wanted = _tokens(text)
    if len(wanted) < _TEXT_MIN_SHARED:
        return None
    best: list[str] = []
    best_score = 0.0
    for commit in commits:
        shared = wanted & _tokens(commit.title)
        score = len(shared) / len(wanted)
        if len(shared) < _TEXT_MIN_SHARED or score < _TEXT_MIN_COVERAGE or score < best_score:
            continue
        best = [commit.sha] if score > best_score else [*best, commit.sha]
        best_score = score
    if not best:
        return None
    return sorted(best), round(min(_CONFIDENCE_TEXT_CAP, _CONFIDENCE_TEXT_CAP * best_score), 3)


def _closing_issues(prs: list[int], items: dict[int, GhItem]) -> set[int]:
    """Return the issue numbers GitHub says the given pull requests close."""
    return {number for pr in prs if (item := items.get(pr)) is not None and item.kind == "pr" for number in item.linked}


class _Linker:
    """Resolve changelog entries to commits: SHA, PR (local, then gh), issue (gh), text match, unlinked."""

    def __init__(self, releases: dict[str, _Release], objects: _GitObjects, gh: _GhSession) -> None:
        """Index local PR numbers per release from commit messages."""
        self.releases = releases
        self.objects = objects
        self.gh = gh
        self.local: dict[int, list[str]] = {}
        self.releases_of: dict[str, set[str]] = {}
        self._via_gh: dict[str, dict[int, list[str]]] = {}
        for release in releases.values():
            for commit in release.commits:
                self.releases_of.setdefault(commit.sha, set()).add(release.version)
                for number in (commit.pr, commit.reapplies):
                    if number is not None and commit.sha not in self.local.get(number, []):
                        self.local.setdefault(number, []).append(commit.sha)

    def _gh_pr_commits(self, version: str, number: int) -> list[str]:
        """Return the commits of release ``version`` that GitHub associates with pull request ``number``.

        A rebase-merged pull request lands one commit per original commit, none of them naming the number, and GitHub
        reports only the last as the merge commit. Asking GitHub per commit of the release, once, finds the rest.
        Commits that already name their pull request are not asked about.
        """
        if version not in self._via_gh:
            unnamed = [commit.sha for commit in self.releases[version].commits if commit.pr is None]
            mapping: dict[int, list[str]] = {}
            for sha, found in self.gh.commit_prs(unnamed).items():
                for found_number in found:
                    mapping.setdefault(found_number, []).append(sha)
            self._via_gh[version] = mapping
        return self._via_gh[version].get(number, [])

    def _prefer_own(self, draft: _Draft, commits: list[str]) -> list[str]:
        """Keep commits inside the draft's own release range; else those shipped in an earlier release; else none.

        A backported ``(#12)`` thus resolves to its own cherry-pick, a fix citing an earlier feature PR next to its own
        PR links to its own commit, and an entry never links to a commit first shipped after its release.
        """
        own = [sha for sha in commits if draft.release in self.releases_of.get(sha, ())]
        limit = self.releases[draft.release].order
        earlier = [
            sha for sha in commits if any(self.releases[rel].order <= limit for rel in self.releases_of.get(sha, ()))
        ]
        return own or earlier

    def link_all(self, drafts: list[_Draft]) -> None:
        """Run the ladder over every changelog draft and attach closing-issue numbers for text references."""
        changelog = [draft for draft in drafts if draft.source == SOURCE_CHANGELOG]
        resolved = self.objects.existing_commits(token for draft in changelog for token in draft.sha_tokens)
        items = self.gh.items(n for draft in drafts for n in [*draft.refs.prs, *draft.refs.issues])
        existing = self._local_gh_commits(items)
        for draft in changelog:
            self._link(draft, resolved, items, existing)
        for draft in drafts:
            draft.closing = _closing_issues(draft.refs.prs, items)

    def _local_gh_commits(self, items: dict[int, GhItem]) -> set[str]:
        """Return the commits GitHub named (merge, PR and closing-PR commits) that exist in the local clone."""
        oids = {oid for item in items.values() for oid in (item.merge_commit, *item.commits, *item.linked_commits)}
        return set(self.objects.existing_commits(oid for oid in oids if oid).values())

    def _link(self, draft: _Draft, resolved: dict[str, str], items: dict[int, GhItem], existing: set[str]) -> None:
        """Apply the ladder to one draft; the first rung with commits wins."""
        draft.refs.shas = sorted({resolved[token] for token in draft.sha_tokens if token in resolved})
        gh_issues = [
            n for n in draft.refs.prs if not self.local.get(n) and (item := items.get(n)) and item.kind == "issue"
        ]
        draft.refs.prs = [n for n in draft.refs.prs if n not in gh_issues]
        draft.refs.issues = sorted({*draft.refs.issues, *gh_issues})
        rungs = (
            ("sha", draft.refs.shas),
            ("pr", self._pr_commits(draft, items, existing)),
            ("issue", self._issue_commits(draft, items, existing)),
        )
        for method, commits in rungs:
            if commits:
                draft.commits, draft.link_method, draft.link_confidence = (
                    sorted(set(commits)),
                    method,
                    _CONFIDENCE_EXACT,
                )
                return
        hit = _text_match(draft.text, self.releases[draft.release].commits)
        if hit is not None:
            draft.commits, draft.link_confidence = hit
            draft.link_method = "text"

    def _pr_commits(self, draft: _Draft, items: dict[int, GhItem], existing: set[str]) -> list[str]:
        """Return commits of the draft's PRs: local message matches first, then the gh merge commit or PR commits."""
        commits: list[str] = []
        for number in draft.refs.prs:
            local = self.local.get(number, [])
            item = items.get(number)
            if local:
                commits += local
            elif item is not None and item.kind == "pr":
                merge = [item.merge_commit] if item.merge_commit in existing else []
                commits += merge or [sha for sha in item.commits if sha in existing]
                commits += self._gh_pr_commits(draft.release, number)
        return self._prefer_own(draft, commits)

    def _issue_commits(self, draft: _Draft, items: dict[int, GhItem], existing: set[str]) -> list[str]:
        """Return local merge commits of the pull requests that closed the draft's issues (gh only)."""
        commits = [
            sha
            for number in draft.refs.issues
            if (item := items.get(number)) is not None and item.kind == "issue"
            for sha in item.linked_commits
            if sha in existing
        ]
        return self._prefer_own(draft, commits)


# --------------------------------------------------------------------------------------------------------------------
# Assembly: duplicates, backport aliases, footprints, text references
# --------------------------------------------------------------------------------------------------------------------


def _absorb(target: _Draft, other: _Draft, *, take_commits: bool) -> None:
    """Fold ``other`` into ``target``: its release becomes secondary, its references (and optionally commits) merge."""
    if other.release != target.release and other.release not in target.secondary_releases:
        target.secondary_releases.append(other.release)
    target.secondary_releases += [rel for rel in other.secondary_releases if rel not in target.secondary_releases]
    target.refs.prs = sorted({*target.refs.prs, *other.refs.prs})
    target.refs.issues = sorted({*target.refs.issues, *other.refs.issues})
    target.closing |= other.closing
    if take_commits:
        target.commits = sorted({*target.commits, *other.commits})
        target.patch_ids = sorted({*target.patch_ids, *other.patch_ids})


def _merge_same_ids(drafts: list[_Draft], order_of: dict[str, int]) -> list[_Draft]:
    """Merge drafts sharing one ID (a heuristic commit shipped in several releases) into the earliest release's."""
    kept: dict[str, _Draft] = {}
    for draft in sorted(drafts, key=lambda d: (order_of[d.release], d.position)):
        if draft.entry_id in kept:
            _absorb(kept[draft.entry_id], draft, take_commits=True)
        else:
            kept[draft.entry_id] = draft
    return list(kept.values())


def _fold_aliases(drafts: list[_Draft], order_of: dict[str, int]) -> tuple[list[_Draft], dict[str, str]]:
    """Fold backport-equivalent entries (same kind, same non-empty patch-id set, different release) into one.

    The canonical member is in the earliest release by index order; entries of that same release all stay canonical
    (several entries may cite one PR). A later member aliases to the canonical with the same normalised text, else to
    the first canonical in file order. Columns also take the alias's commits into their footprint.
    """
    groups: dict[tuple[str, frozenset[str]], list[_Draft]] = {}
    for draft in drafts:
        if draft.patch_ids:
            groups.setdefault((draft.kind, frozenset(draft.patch_ids)), []).append(draft)
    aliases: dict[str, str] = {}
    for members in groups.values():
        members.sort(key=lambda d: (order_of[d.release], d.position))
        canonical = [d for d in members if d.release == members[0].release]
        for other in members[len(canonical) :]:
            target = next((d for d in canonical if entry_hash(d.text) == entry_hash(other.text)), canonical[0])
            _absorb(target, other, take_commits=target.kind != "fix")
            target.aliases.append(other.entry_id)
            aliases[other.entry_id] = target.entry_id
    return [draft for draft in drafts if draft.entry_id not in aliases], aliases


def _fold_covered_heuristics(drafts: list[_Draft], order_of: dict[str, int], aliases: dict[str, str]) -> list[_Draft]:
    """Fold a heuristic column into the one changelog column of its kind whose commits include the heuristic commit.

    A release without a changelog section yields a ``commit/<patch-id>`` column from the commit that a later release's
    changelog entry also names (usually with the pull request's follow-up commits, so patch-id sets differ and the
    backport fold misses it). One feature then appears as two columns and one of them is always a false link. The
    earlier release stays canonical, like the backport fold. A commit named by several changelog entries is ambiguous
    and folds nothing. Fixes keep their IDs because labels and rows reference them. ``aliases`` is updated in place.
    """
    changelog = [d for d in drafts if d.source == SOURCE_CHANGELOG and d.kind != "fix"]
    folded: set[str] = set()
    for heuristic in (d for d in drafts if d.source == SOURCE_HEURISTIC and d.kind != "fix"):
        owners = [d for d in changelog if d.kind == heuristic.kind and set(heuristic.commits) <= set(d.commits)]
        if len(owners) != 1 or owners[0].release == heuristic.release:
            continue
        owner = owners[0]
        target, other = sorted((owner, heuristic), key=lambda d: order_of[d.release])
        _absorb(target, other, take_commits=True)
        target.aliases += [other.entry_id, *other.aliases]
        for alias in (other.entry_id, *other.aliases):
            aliases[alias] = target.entry_id
        folded.add(other.entry_id)
    return [d for d in drafts if d.entry_id not in folded]


def _attach_footprints(repo: Path, columns: list[_Draft], objects: _GitObjects) -> None:
    """Fill files and symbols of every column from its commits (features: added symbols; changes: added or changed)."""
    for column in columns:
        column.files = sorted({path for sha in column.commits for path in objects.changed_files(sha)})
        added_only = column.kind == "feature"
        column.symbols = sorted(
            {name for sha in column.commits for name in python_symbols_changed(repo, sha, added_only=added_only)}
        )


def _expand_merged_commits(columns: list[_Draft], objects: _GitObjects) -> None:
    """Add the branch commits of every merge commit in a column, so blame that lands on them reaches the feature.

    Runs after the footprint is read: files and symbols stay those of the merge diff, only the commit set grows.
    """
    for column in columns:
        merged = [sha for commit in column.commits for sha in objects.merged_commits(commit)]
        column.commits = list(dict.fromkeys([*column.commits, *merged]))


def _version_lookup(releases: dict[str, _Release]) -> dict[Any, str]:
    """Map canonical text and PEP 440 sort key of every tagged release to its index version."""
    lookup: dict[Any, str] = {}
    for version in releases:
        parsed = parse_version(version) if version != UNRELEASED else None
        if parsed is not None:
            lookup[version] = version
            lookup.setdefault(parsed.sort_key(), version)
    return lookup


def _match_version(token: str, lookup: dict[Any, str]) -> str | None:
    """Return the index version a version token names, or None."""
    parsed = parse_version(token)
    if parsed is None:
        return None
    return lookup.get(parsed.canonical) or lookup.get(parsed.sort_key())


def _attach_text_refs(
    fixes: list[_Draft], columns: list[_Draft], order_of: dict[str, int], lookup: dict[Any, str]
) -> None:
    """Link fixes to columns sharing a PR/issue number, and record regression versions, at or before the fix release."""
    by_number: dict[int, list[_Draft]] = {}
    for column in columns:
        for number in {*column.refs.prs, *column.refs.issues}:
            by_number.setdefault(number, []).append(column)
    for fix in fixes:
        limit = order_of[fix.release]
        numbers = {*fix.refs.prs, *fix.refs.issues, *fix.closing}
        fix.text_refs = sorted(
            {col.entry_id for n in numbers for col in by_number.get(n, []) if order_of[col.release] <= limit}
        )
        versions = {found for token in fix.version_tokens if (found := _match_version(token, lookup))}
        fix.refs.versions = sorted((v for v in versions if order_of[v] <= limit), key=lambda v: order_of[v])


def _record(draft: _Draft, order_of: dict[str, int]) -> dict[str, Any]:
    """Serialise a draft to the JSON entry shape of the evolution contract."""
    return {
        "entry_id": draft.entry_id,
        "kind": draft.kind,
        "release": draft.release,
        "line": draft.line,
        "text": draft.text,
        "source": draft.source,
        "refs": {
            "shas": sorted(set(draft.refs.shas)),
            "prs": sorted(set(draft.refs.prs)),
            "issues": sorted(set(draft.refs.issues)),
            "versions": list(draft.refs.versions),
        },
        "commits": sorted(set(draft.commits)),
        "link_method": draft.link_method,
        "link_confidence": draft.link_confidence,
        "patch_ids": sorted(set(draft.patch_ids)),
        "files": list(draft.files),
        "symbols": list(draft.symbols),
        "secondary_releases": sorted(set(draft.secondary_releases), key=lambda v: order_of[v]),
        "aliases": sorted(set(draft.aliases)),
        "text_refs": list(draft.text_refs),
    }


# --------------------------------------------------------------------------------------------------------------------
# Changelog sections per release
# --------------------------------------------------------------------------------------------------------------------


def _merge_buckets(target: Buckets, extra: Buckets) -> None:
    """Append ``extra``'s bullets to ``target`` bucket by bucket."""
    for bucket in _BUCKETS:
        target[bucket] += extra.get(bucket, [])


def _map_sections(parsed: dict[str, Buckets], releases: dict[str, _Release], warnings: list[str]) -> dict[str, Buckets]:
    """Assign parsed changelog sections to index versions.

    A section matches its release by canonical version, else by PEP 440 equality (``1.2`` == ``1.2.0``). A section newer
    than every tag is treated as unreleased; any other section without a tag is skipped with a warning.
    """
    lookup = _version_lookup(releases)
    newest = max((key for key in lookup if isinstance(key, tuple)), default=None)
    mapped: dict[str, Buckets] = {}
    for version, buckets in parsed.items():
        target = UNRELEASED if version == UNRELEASED else _match_version(version, lookup)
        parsed_version = parse_version(version) if target is None else None
        if target is None and parsed_version is not None and (newest is None or parsed_version.sort_key() > newest):
            warnings.append(f"changelog section {version} is newer than every release tag; treated as unreleased")
            target = UNRELEASED
        if target is None:
            warnings.append(f"changelog section {version} has no matching release tag; its entries are skipped")
            continue
        _merge_buckets(mapped.setdefault(target, {bucket: [] for bucket in _BUCKETS}), buckets)
    return mapped


def _fragment_buckets(root: Path, fragments: list[str]) -> Buckets:
    """Turn towncrier fragments into unreleased buckets; a numeric fragment name becomes a ``(#N)`` reference."""
    buckets: Buckets = {bucket: [] for bucket in _BUCKETS}
    for rel in fragments:
        path = _inside(root, rel)
        match = _FRAGMENT_NAME.match(PurePosixPath(rel).name)
        bucket = _FRAGMENT_BUCKET.get(match.group("type").lower()) if match else None
        if path is None or match is None or bucket is None:
            continue
        text = " ".join(read_bounded_text(path, errors="replace").split())
        ref = match.group("ref")
        if text:
            buckets[bucket].append(f"{text} (#{ref})" if ref.isdigit() else text)
    return buckets


def _load_sections(
    root: Path, found: dict[str, Any], releases: dict[str, _Release], warnings: list[str]
) -> dict[str, Buckets]:
    """Read, parse and map the changelog; add towncrier fragments as unreleased entries when no such section exists."""
    parsed: dict[str, Buckets] = {}
    if found["path"] is None:
        warnings.append("no changelog file found; every release uses heuristic commit classification")
    else:
        text = read_bounded_text(root / found["path"], errors="replace")
        parsed = parse_changelog(text, found["format"], warnings=warnings)
    sections = _map_sections(parsed, releases, warnings)
    towncrier = found.get("towncrier") or {}
    if UNRELEASED not in sections and towncrier.get("fragments"):
        fragments = _fragment_buckets(root, towncrier["fragments"])
        if any(fragments.values()):
            sections[UNRELEASED] = fragments
    return sections


def _add_release_bodies(
    sections: dict[str, Buckets], releases: dict[str, _Release], gh: _GhSession, warnings: list[str]
) -> None:
    """Fill releases that have no changelog entries from their GitHub Release notes, the last changelog source.

    The notes are parsed like a one-release changelog: headings are demoted a level so a leading ``#`` title cannot end
    the section, GitHub's ``What's Changed`` list is read bullet by bullet, and a release whose notes yield no entry
    stays uncovered (heuristic commit classification).
    """
    missing = [
        release
        for release in releases.values()
        if release.version != UNRELEASED and release.tag and not any((sections.get(release.version) or {}).values())
    ]
    if not missing or not gh.active:
        return
    bodies = gh.release_bodies()
    used: list[str] = []
    for release in sorted(missing, key=lambda item: item.order):
        body = bodies.get(release.tag)
        if not body:
            continue
        demoted = re.sub(r"(?m)^(#{1,5})(?=\s)", r"#\1", body.replace("\r\n", "\n"))
        buckets = parse_changelog(f"# {release.version}\n\n{demoted}\n", "md").get(release.version)
        if buckets and any(buckets.values()):
            sections[release.version] = buckets
            used.append(release.version)
    if used:
        shown = ", ".join(used[:_WARN_LIST_MAX]) + (", ..." if len(used) > _WARN_LIST_MAX else "")
        warnings.append(f"{len(used)} release(s) without a changelog section read from GitHub Release notes: {shown}")


def _draft_all(
    releases: dict[str, _Release],
    sections: dict[str, Buckets],
    gh: _GhSession,
    objects: _GitObjects,
    context: tuple[str | None, list[str]],
) -> list[_Draft]:
    """Draft entries per release: changelog bullets where a non-empty section exists, heuristic commits otherwise."""
    slug, warnings = context
    ordered = sorted(releases.values(), key=lambda release: release.order)
    uncovered = [release for release in ordered if not any((sections.get(release.version) or {}).values())]
    uncovered_versions = {release.version for release in uncovered}
    empty = [release.version for release in uncovered if release.version in sections]
    if empty:
        shown = ", ".join(empty[:_WARN_LIST_MAX]) + (", ..." if len(empty) > _WARN_LIST_MAX else "")
        warnings.append(
            f"{len(empty)} changelog sections have no Added/Changed/Fixed entries (no bullet sits in such a subsection "
            f"or names its kind in its first words); heuristic commit classification used for: {shown}"
        )
    label_kinds = _label_kinds(uncovered, gh)
    drafts: list[_Draft] = []
    for release in ordered:
        if release.version in uncovered_versions:
            drafts += _heuristic_drafts(release, label_kinds, objects, slug)
        else:
            drafts += _changelog_drafts(release, sections[release.version], slug)
    return drafts


def _github_session(root: Path, gh_available: bool, github: GitHubLookup | None, warnings: list[str]) -> _GhSession:
    """Return the GitHub session: ``github`` (or the ``gh`` CLI) when available, else a disabled one plus a warning."""
    if not gh_available:
        warnings.append("gh unavailable: PR/issue numbers resolved from local commit messages only; gh rungs skipped")
        return _GhSession(None, warnings)
    return _GhSession(github if github is not None else _GhCli(root), warnings)


def build_entries(
    repo: Path | str, index: dict[str, Any], *, gh_available: bool, github: GitHubLookup | None = None
) -> dict[str, Any]:
    """Build linked fix rows and feature/change columns for every indexed release.

    Args:
        repo: Repository root.
        index: Release index (``evolution_releases.build_release_index`` shape); reads ``releases``,
            ``commit_releases`` (per-line release ranges) and ``head``.
        gh_available: Whether GitHub lookups may run; False skips the gh rungs and PR labels with a warning.
        github: Lookup implementation to use when ``gh_available`` (default: the ``gh`` CLI). Tests pass a fake.

    Returns:
        ``{"fixes": [Entry], "columns": [Entry], "aliases": {alias_id: canonical_id}, "changelog": {"path",
        "format"}, "warnings": [str]}``. Backport aliases are folded: only canonical entries appear in the lists.

    Raises:
        GitError: a git command failed (``GitTimeout`` when it timed out).
        KeyError: the index lacks ``releases`` or a release field.
        ValueError: the index holds a malformed commit SHA.
    """
    root = Path(repo)
    warnings: list[str] = []
    found = discover_changelog(root)
    releases = _build_releases(root, index)
    order_of = {version: release.order for version, release in releases.items()}
    gh = _github_session(root, gh_available, github, warnings)
    objects = _GitObjects(root, releases)
    slug = _origin_slug(root)
    sections = _load_sections(root, found, releases, warnings)
    _add_release_bodies(sections, releases, gh, warnings)
    drafts = _draft_all(releases, sections, gh, objects, (slug, warnings))
    _Linker(releases, objects, gh).link_all(drafts)
    for draft in drafts:
        draft.patch_ids = draft.patch_ids or objects.patch_ids(draft.commits)
    drafts, aliases = _fold_aliases(_merge_same_ids(drafts, order_of), order_of)
    drafts = _fold_covered_heuristics(drafts, order_of, aliases)
    drafts.sort(key=lambda d: (order_of[d.release], d.position, d.entry_id))
    fixes = [draft for draft in drafts if draft.kind == "fix"]
    columns = [draft for draft in drafts if draft.kind != "fix"]
    _attach_footprints(root, columns, objects)
    _expand_merged_commits(columns, objects)
    _attach_text_refs(fixes, columns, order_of, _version_lookup(releases))
    return {
        "fixes": [_record(draft, order_of) for draft in fixes],
        "columns": [_record(draft, order_of) for draft in columns],
        "aliases": dict(sorted(aliases.items())),
        "changelog": {"path": found["path"], "format": found["format"]},
        "warnings": warnings,
    }


# --------------------------------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser with the ``discover`` and ``entries`` subcommands."""
    parser = argparse.ArgumentParser(
        prog="evolution_changelog.py", description="Discover and parse a changelog; build linked evolution entries."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover", help="print the changelog path, format and towncrier setup as JSON")
    discover.add_argument("--repo", type=Path, required=True, help="repository root")
    entries = sub.add_parser("entries", help="write fixes, columns and aliases for every indexed release")
    entries.add_argument("--repo", type=Path, required=True, help="repository root")
    entries.add_argument("--index", type=Path, required=True, help="release index JSON (evolution_releases.py)")
    entries.add_argument("--out", type=Path, required=True, help="output JSON file")
    entries.add_argument("--no-gh", action="store_true", help="skip GitHub lookups even when gh is installed")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: Arguments; None reads ``sys.argv``.

    Returns:
        0 on success, 1 for invalid arguments or a malformed index, 2 for a failed git command.
    """
    args = build_parser().parse_args(argv)
    try:
        if args.command == "discover":
            print(canonical_json(discover_changelog(args.repo)))
            return 0
        index = json.loads(read_bounded_text(args.index))
        result = build_entries(args.repo, index, gh_available=not args.no_gh and which("gh") is not None)
        atomic_write_json(args.out, result)
    except GitError as exc:
        print(f"Error: git failed: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(
        f"entries: {len(result['fixes'])} fixes, {len(result['columns'])} columns, {len(result['aliases'])} aliases, "
        f"{len(result['warnings'])} warnings -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
