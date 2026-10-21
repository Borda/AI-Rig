#!/usr/bin/env python3
"""Reject tracked-file references to this repository's gitignored documents.

Purpose:
    Prevent shipped contracts from depending on private evidence unavailable to
    other clones while allowing normal runtime-artifact examples.
Scope:
    Scan explicit tracked text files for concrete paths under watched private
    directories such as ``.plans/`` and ``docs/specs/``. Templates, globs,
    variables, nonexistent examples, and reviewed same-line waivers pass. Files
    larger than ``MAX_FILE_BYTES`` and targets resolving outside the checkout
    are skipped: neither can be a private document of this repository.
Usage:
    ``python3 plugins/cc_foundry/bin/check_gitignored_refs.py <files...>``. The
    repository pre-commit configuration supplies changed text files.
Outputs:
    Print each source line and ignored target for a violation; stay silent when
    clean. The command does not modify the checkout.
Failure:
    Exit 1 for violations. Git discovery or ``check-ignore`` execution errors
    fail closed through an exception and a nonzero process status.
Used by:
    The repository ``check-gitignored-refs`` pre-commit hook and Foundry's test
    suite.

The plugins deliberately instruct *target projects* to keep runtime artifacts in
dot-directories, so mentioning those conventions — templates like
``.plans/active/todo_<name>.md`` or illustrative README paths — is normal. A
tracked file must not cite a concrete document that exists only in this
checkout's private ignored folders: every other clone would see a dangling
pointer and silently lose its authority.

A generic regex cannot tell those two cases apart, so this check uses the
checkout itself as context. A candidate reference is a violation only when all
three hold:

1. the path contains no placeholder, glob, or shell variable (the token regex
   only matches literal path characters, so templated paths never qualify);
2. the path resolves to an existing regular file in this checkout, tried
   relative to the repository root and to the referencing file's directory;
3. ``git check-ignore`` confirms git ignores that file.

Illustrative documentation paths fail condition 2, so no allowlist is needed.
A deliberate exception can carry the marker ``gitignored-ref-ok`` on the same
line.

Residual limit: a reference whose target was already deleted (a dangling
pointer) is not detected — the check fires at authoring time, while the cited
private file still exists on the author's machine, which is when such
references are written.

Examples:
    Run against explicit files (as pre-commit does)::

        python3 plugins/cc_foundry/bin/check_gitignored_refs.py README.md

    Exit status is 0 when clean, 1 when any violation is found.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

#: Directories holding private evidence only — never legitimate runtime-output
#: conventions — extend this tuple to widen the check.
WATCHED_DIRS = (r"\.plans", r"docs[\\/]specs")

#: Longest directory prefix a token may carry before its watched directory. Unbounded repetition makes the scan
#: quadratic on a long ``a/a/a/...`` line (12 s at 32000 segments); a path deeper than this keeps its last segments.
MAX_PREFIX_SEGMENTS = 32

_PREFIX_RE = r"(?:[\w.-]+[\\/]){0," + str(MAX_PREFIX_SEGMENTS) + "}"

#: Matches a literal repo path into a watched directory. Placeholder characters
#: (``<>{}$*``) are absent from the classes, so a templated path truncates and
#: then fails the existing-file test instead of matching.
TOKEN_RE = re.compile(r"(?<![\w.-])" + _PREFIX_RE + r"(?:" + "|".join(WATCHED_DIRS) + r")[\\/][\w./\\-]*\w")

#: Tracked documents citing private evidence are small; a larger file is generated or binary-like content.
MAX_FILE_BYTES = 16 * 1024 * 1024

#: Upper bound for one git call; a hung git fails closed through ``subprocess.TimeoutExpired``.
GIT_TIMEOUT_SECONDS = 30

#: Same-line escape hatch for deliberate, reviewed references.
WAIVER_MARKER = "gitignored-ref-ok"

#: This exact source is exempt because its contract necessarily names watched paths.
SELF_PATH = Path(__file__).resolve()


def repo_root() -> Path:
    """Return the git repository root for the current working directory."""
    output = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],  # noqa: S607 - git resolved via PATH on purpose
        check=True,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    ).stdout.strip()
    return Path(output)


def candidate_tokens(line: str) -> list[str]:
    """Extract literal watched-directory path tokens from one text line."""
    if WAIVER_MARKER in line:
        return []
    return [match.group(0).rstrip("./") for match in TOKEN_RE.finditer(line)]


def resolve_existing_file(token: str, root: Path, source_dir: Path) -> Path | None:
    """Return the existing regular file a token resolves to, if any.

    Tries the token relative to the repository root first (the dominant way these references are written), then relative
    to the referencing file's own directory. A file outside the checkout (reached through ``..`` or a symlink) is never
    this repository's private document, and ``git check-ignore`` rejects it with status 128, so it does not qualify.
    """
    portable_token = token.replace("\\", "/")
    resolved_root = root.resolve()
    for base in (root, source_dir):
        candidate = base / portable_token
        if candidate.is_file() and candidate.resolve().is_relative_to(resolved_root):
            return candidate
    return None


def is_ignored(path: Path, root: Path) -> bool:
    """Return True when git ignores ``path`` in this checkout."""
    result = subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "check-ignore", "-q", "--", str(path)],  # noqa: S607 - git resolved via PATH on purpose
        check=False,
        capture_output=True,
        cwd=root,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(f"git check-ignore failed for {path} with status {result.returncode}")


def scan_file(source: Path, root: Path) -> list[str]:
    """Return violation messages for one tracked text file."""
    try:
        if source.stat().st_size > MAX_FILE_BYTES:
            return []
        text = source.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    violations: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for token in candidate_tokens(line):
            target = resolve_existing_file(token, root, source.parent)
            if target is not None and is_ignored(target, root):
                violations.append(f"{source}:{line_number}: refers to gitignored {token}")
    return violations


def main(argv: list[str]) -> int:
    """Scan the given files; print violations and return the exit status."""
    root = repo_root()
    violations: list[str] = []
    for name in argv:
        source = Path(name)
        if source.resolve() == SELF_PATH or not source.is_file():
            continue
        violations.extend(scan_file(source, root))
    for violation in violations:
        print(violation)
    if violations:
        print(
            "Tracked files must not depend on this repository's gitignored documents; "
            f"move the content into a tracked file or mark a reviewed exception with '{WAIVER_MARKER}'."
        )
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
