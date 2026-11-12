#!/usr/bin/env python
"""Scan a repository's documentation for "unreleased" leftovers before a release ships.

Purpose: A release can ship docs that still describe a feature as unreleased, coming in the next release, or added in a
version that does not exist yet. This helper finds those leftovers deterministically so the release audit and prepare
flows can block on them instead of relying on a reader to notice.

Scope: Read-only scan below one repository root; inside a git work tree the file list comes from ``git ls-files``
(tracked plus untracked, ``.gitignore``-d files left out), elsewhere from a filesystem walk. Scanned files are
``README*`` at any depth, everything under a ``docs/`` directory (binary files skipped), and any ``*.md``, ``*.rst``
or ``*.txt`` file. Dependency, tooling, build and artifact trees are skipped (``node_modules``, ``.venv``, ``venv``,
``vendor``, ``.git``, ``.tox``, ``.nox``, ``__pycache__``, ``site-packages``, Sphinx ``_build``, the release-flow
artifact directories ``.temp``, ``.reports``, ``.cache``, ``.plans``, ``.notes``) as is the
top-level ``releases/`` directory that ``/oss:release prepare`` writes. Symlinks are never followed.

Markers reported in ordinary docs (one row per line, the first marker that matches):

- a Sphinx or MyST ``versionadded`` / ``versionchanged`` / ``deprecated`` directive whose argument is ``unreleased``,
  ``next`` or ``dev`` (or starts with ``dev``), or a version greater than the target version;
- the word ``unreleased`` in any case;
- the phrases ``to be released``, ``upcoming release``, ``next release``, ``coming soon``, ``not yet released``;
- ``TBD`` or ``TODO`` directly beside a version number or the word ``version``.

Changelog files (``CHANGELOG*``, or a ``CHANGES``/``HISTORY``/``NEWS`` basename, plus ``--changelog``) are
release history, so the phrase markers above are not applied to them. Instead one rule applies: once a heading for the
target version exists, an ``Unreleased`` heading that still holds entries is a hit. While the target heading does not
exist yet (notes drafting), the Unreleased section is expected and is not reported.

Usage: ``scan_unreleased_leftovers.py --repo-root DIR [--version X.Y.Z] [--changelog PATH]``. Without ``--version``
the version comparison and the changelog rule are skipped; every other marker still applies.

Outputs: One ``path:line: <marker>`` row per hit on stdout, paths relative to the root in POSIX form, sorted. The
marker is a fixed label (plus a directive's short argument), never the document's own text. A one-line count summary
goes to stderr.

Exit codes: 0 no hits; 1 at least one hit; 2 invalid arguments (missing root, unparsable version).

Used by: ``skills/release/modes/audit.md`` (readiness check) and ``skills/release/modes/prepare.md`` (post-changelog
gate).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

#: Directory names never descended into, at any depth.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "vendor",
        ".git",
        ".tox",
        ".nox",
        "__pycache__",
        "site-packages",
        "_build",
        ".temp",
        ".reports",
        ".cache",
        ".plans",
        ".notes",
    }
)

#: Top-level directories skipped only at the repository root (release-flow output).
_SKIP_ROOT_DIRS: frozenset[str] = frozenset({"releases"})

_DOC_SUFFIXES: frozenset[str] = frozenset({".md", ".rst", ".txt"})
#: Changelog basenames besides any ``CHANGELOG*`` file (compared without extension, case-insensitive).
_CHANGELOG_STEMS: frozenset[str] = frozenset({"changes", "history", "news"})

#: Files larger than this are not documentation worth scanning line by line.
_MAX_BYTES = 2_000_000

_VERSION_NUMBER = r"v?\d+(?:\.\d+)+"
_DIRECTIVE = re.compile(r"(?:\.\.\s+|\{)(versionadded|versionchanged|deprecated)(?:::|\})\s*([A-Za-z0-9._+-]+)")
_PHRASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("unreleased", re.compile(r"\bunreleased\b", re.IGNORECASE)),
    ("to be released", re.compile(r"\bto\s+be\s+released\b", re.IGNORECASE)),
    ("upcoming release", re.compile(r"\bupcoming\s+release\b", re.IGNORECASE)),
    ("next release", re.compile(r"\bnext\s+release\b", re.IGNORECASE)),
    ("coming soon", re.compile(r"\bcoming\s+soon\b", re.IGNORECASE)),
    ("not yet released", re.compile(r"\bnot\s+yet\s+released\b", re.IGNORECASE)),
    (
        "TBD/TODO beside a version",
        re.compile(
            rf"(?:\b{_VERSION_NUMBER}|\b[Vv]ersion)\W{{0,4}}\b(?:TBD|TODO)\b"
            rf"|\b(?:TBD|TODO)\b\W{{0,4}}(?:\b{_VERSION_NUMBER}|\b[Vv]ersion)\b"
        ),
    ),
)
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+])\1{2,}\s*$")
_HEADING_VERSION = re.compile(r"^\[?(?:(?:version|release)\s+)?v?(\d+(?:\.\d+)*)\b", re.IGNORECASE)
_HEADING_UNRELEASED = re.compile(r"^\[?unreleased\]?\b", re.IGNORECASE)
_LINK_REFERENCE = re.compile(r"^\s*\[[^\]]+\]:\s*\S+")


def parse_version(text: str) -> tuple[int, ...] | None:
    """Return the numeric release segment of ``text`` with trailing zeros dropped, or ``None`` when it has none.

    Args:
        text: A version string such as ``v1.2.0``, ``1.2`` or ``1.3.0rc1``.

    Returns:
        Integer tuple without trailing zero components, so ``1.2`` and ``1.2.0`` compare equal.

    Examples:
        >>> parse_version("v1.2.0")
        (1, 2)
        >>> parse_version("1.10.3rc1")
        (1, 10, 3)
        >>> parse_version("next") is None
        True
    """
    match = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    if not match:
        return None
    parts = [int(p) for p in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _directive_marker(line: str, target: tuple[int, ...] | None) -> str | None:
    """Return a marker for a version directive whose argument names an unreleased version, else ``None``."""
    match = _DIRECTIVE.search(line)
    if not match:
        return None
    name, argument = match.group(1), match.group(2)
    lowered = argument.lower()
    if lowered in {"unreleased", "next"} or lowered.startswith("dev"):
        return f"{name}:: {argument}"
    version = parse_version(argument)
    if target is not None and version is not None and version > target:
        return f"{name}:: {argument} newer than target"
    return None


def line_marker(line: str, target: tuple[int, ...] | None) -> str | None:
    """Return the first leftover marker found on one documentation line, or ``None``.

    Args:
        line: One line of a documentation file.
        target: Parsed target version, or ``None`` when no version was given.

    Returns:
        A fixed marker label.

    Examples:
        >>> line_marker(".. versionadded:: next", None)
        'versionadded:: next'
        >>> line_marker("Shipped in the 1.2 release.", (1, 2)) is None
        True
    """
    directive = _directive_marker(line, target)
    if directive:
        return directive
    for label, pattern in _PHRASES:
        if pattern.search(line):
            return label
    return None


def _headings(lines: list[str]) -> Iterator[tuple[int, int, str]]:
    """Yield ``(index, level, title)`` for every Markdown or reStructuredText heading in ``lines``."""
    rst_levels: list[str] = []
    for index, line in enumerate(lines):
        match = _MD_HEADING.match(line)
        if match:
            yield index, len(match.group(1)), match.group(2).strip()
            continue
        if index + 1 < len(lines) and line.strip() and _RST_UNDERLINE.match(lines[index + 1]):
            char = lines[index + 1].strip()[0]
            if char not in rst_levels:
                rst_levels.append(char)
            yield index, rst_levels.index(char) + 1, line.strip()


def _section_has_entries(lines: list[str], start: int, end: int, heading_rows: set[int]) -> bool:
    """Return whether a section carries content beyond sub-headings (an empty ``### Added`` skeleton) and link refs."""
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if index in heading_rows or not stripped:
            continue
        if not _RST_UNDERLINE.match(stripped) and not _LINK_REFERENCE.match(stripped):
            return True
    return False


def changelog_hits(lines: list[str], target: tuple[int, ...] | None) -> list[tuple[int, str]]:
    """Return ``(line_number, marker)`` for an Unreleased section still holding entries once the target heading exists.

    Args:
        lines: Changelog lines.
        target: Parsed target version; ``None`` disables the rule.

    Returns:
        One-based line numbers with their marker; empty while the target heading does not exist yet.

    Examples:
        >>> changelog_hits(["## Unreleased", "- a", "## 1.2.0", "- b"], (1, 2))
        [(1, 'changelog Unreleased section holds entries after the target release heading')]
        >>> changelog_hits(["## Unreleased", "- a", "## 1.1.0"], (1, 2))
        []
    """
    if target is None:
        return []
    headings = list(_headings(lines))
    has_target = any(
        (version := _HEADING_VERSION.match(title)) and parse_version(version.group(1)) == target
        for _, _, title in headings
    )
    if not has_target:
        return []
    heading_rows = {index for index, _, _ in headings}
    hits: list[tuple[int, str]] = []
    for position, (index, level, title) in enumerate(headings):
        if not _HEADING_UNRELEASED.match(title):
            continue
        end = next((i for i, lvl, _ in headings[position + 1 :] if lvl <= level), len(lines))
        if _section_has_entries(lines, index, end, heading_rows):
            hits.append((index + 1, "changelog Unreleased section holds entries after the target release heading"))
    return hits


def _is_doc(relative: Path) -> bool:
    """Return whether a repository-relative file path is documentation this scan covers."""
    return (
        relative.name.lower().startswith("readme")
        or "docs" in relative.parts[:-1]
        or relative.suffix.lower() in _DOC_SUFFIXES
    )


def _is_changelog(relative: Path, changelog: Path | None) -> bool:
    """Return whether a file is release history that only the changelog rule applies to."""
    stem = relative.stem.lower()
    return relative == changelog or stem.startswith("changelog") or stem in _CHANGELOG_STEMS


def _git_listed(root: Path) -> list[Path] | None:
    """Return the files git tracks or would track below ``root`` (ignored files left out), or ``None`` without git.

    Respecting ``.gitignore`` keeps local build output (a stale Sphinx ``_build`` tree, a generated site) from holding
    the release gate red after the real docs are fixed. ``core.fsmonitor`` is disabled so the scan never starts a hook
    configured by the scanned repository.
    """
    git = shutil.which("git")
    if git is None:
        return None
    command = [git, "-c", "core.fsmonitor=false", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"]
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=120)  # noqa: S603 - fixed argv
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [Path(name) for name in result.stdout.decode("utf-8", "surrogateescape").split("\0") if name]


def _walked(root: Path) -> Iterator[Path]:
    """Yield every file below ``root`` as a relative path, pruning excluded trees and symlinked directories."""
    for directory, dirnames, filenames in os.walk(root):
        current = Path(directory)
        dirnames[:] = sorted(name for name in dirnames if not (current / name).is_symlink())
        for name in filenames:
            yield (current / name).relative_to(root)


def _excluded(relative: Path) -> bool:
    """Return whether a relative path sits in a dependency, tooling, artifact or release-output tree."""
    directories = relative.parts[:-1]
    return any(part in _SKIP_DIRS for part in directories) or bool(directories and directories[0] in _SKIP_ROOT_DIRS)


def iter_doc_files(root: Path) -> Iterator[Path]:
    """Yield documentation files below ``root`` as relative paths, skipping excluded trees and symlinks.

    Inside a git work tree the candidate list comes from ``git ls-files`` (tracked plus untracked, ignored files left
    out); elsewhere from a filesystem walk.

    Args:
        root: Repository root.

    Yields:
        Paths relative to ``root``, sorted.
    """
    listed = _git_listed(root)
    candidates = listed if listed is not None else list(_walked(root))
    for relative in sorted(set(candidates)):
        path = root / relative
        if _excluded(relative) or not _is_doc(relative) or path.is_symlink() or not path.is_file():
            continue
        yield relative


def _read_lines(path: Path) -> list[str] | None:
    """Return a file's lines, or ``None`` when it is too large, binary (NUL byte) or unreadable."""
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace").splitlines()


def scan(root: Path, target: tuple[int, ...] | None, changelog: Path | None = None) -> tuple[list[str], int]:
    """Scan every documentation file below ``root`` and return the hit rows and the number of files read.

    Args:
        root: Repository root.
        target: Parsed target version, or ``None``.
        changelog: Repository-relative changelog path to treat as release history in addition to the name rule.

    Returns:
        Sorted ``path:line: <marker>`` rows and the count of scanned files.
    """
    rows: list[tuple[str, int, str]] = []
    scanned = 0
    for relative in iter_doc_files(root):
        lines = _read_lines(root / relative)
        if lines is None:
            continue
        scanned += 1
        if _is_changelog(relative, changelog):
            found = changelog_hits(lines, target)
        else:
            found = [(n, m) for n, line in enumerate(lines, 1) if (m := line_marker(line, target))]
        rows.extend((relative.as_posix(), number, marker) for number, marker in found)
    rows.sort()
    return [f"{path}:{number}: {marker}" for path, number, marker in rows], scanned


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", required=True, type=Path, help="Repository root to scan.")
    parser.add_argument("--version", default="", help="Target release version, e.g. 1.3.0 or v1.3.0.")
    parser.add_argument("--changelog", default="", help="Changelog path, relative to the root or absolute.")
    return parser.parse_args(argv)


def _relative_changelog(root: Path, value: str) -> Path | None:
    """Return ``--changelog`` as a root-relative path, or ``None`` when unset or outside the root."""
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        return path
    try:
        return path.resolve().relative_to(root)
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    """Run the scan and print hit rows.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        0 when clean, 1 when any leftover was found, 2 on invalid arguments.
    """
    args = _parse_args(argv)
    root = args.repo_root.resolve()
    if not root.is_dir():
        print(f"error: repository root is not a directory: {args.repo_root}", file=sys.stderr)
        return 2
    target = None
    if args.version:
        target = parse_version(args.version)
        if target is None:
            print(f"error: unparsable --version: {args.version}", file=sys.stderr)
            return 2
    rows, scanned = scan(root, target, _relative_changelog(root, args.changelog))
    for row in rows:
        print(row)
    print(f"unreleased-leftovers: {len(rows)} hit(s) in {scanned} documentation file(s)", file=sys.stderr)
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
