#!/usr/bin/env python3
"""Check that one research:run iteration changed only files inside its declared scope.

Purpose:
    The ideation agent is told which files it may edit (``scope_files``) and that the
    scripts named by ``metric_cmd``/``guard_cmd`` are off limits, but that instruction is
    prompt-only. An agent that edits the evaluator can "improve" the metric without
    improving the code. This helper turns the boundary into a deterministic check that
    runs after the agent returns and before anything is committed.
Scope:
    Reads ``state.json`` (for ``program_file``), the program file's ``## Config``,
    ``## Metric`` and ``## Guard`` fenced blocks, and ``git status --porcelain``. Never
    modifies the working tree; cleanup of a violating iteration stays in the skill.
Usage:
    ``check_iteration_scope.py --state-json .experiments/state/<run-id>/state.json``
Outputs:
    One JSON line on stdout: ``{"changed": [...], "untracked": [...], "violations": [...],
    "checked": true|false}``. ``changed`` lists every changed path (tracked and
    untracked, run-artifact dirs excluded); ``untracked`` is the subset git does not
    track yet; ``violations`` lists paths outside ``scope_files`` or naming an evaluator
    file. ``checked`` is false when there is no program file to read a scope from.
Exit codes:
    0 — no violation (or nothing to check against); 3 — at least one violation;
    1 — git or state.json could not be read; 2 — argparse usage error.
Evaluator set:
    Only tokens of ``metric_cmd``/``guard_cmd`` that name an existing *file* count.
    Directory arguments such as ``tests/`` are not evaluators, so a coverage campaign may
    still create new test files inside ``scope_files``.
Used by:
    research:run Phase 3 (plugins/cc_research/skills/run/SKILL.md).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

#: Run-artifact dirs the loop writes itself; never part of an iteration's change set.
_EXCLUDED_PREFIXES = (".experiments/", ".temp/")

_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_FENCE = re.compile(r"^\s*```")


def _section_blocks(text: str) -> dict[str, list[str]]:
    """Return the first fenced block under each ``## Heading``, keyed by lowercased heading.

    Mirrors research:run's program.md parse rule: find each ``## <Section>`` heading
    (case-insensitive) and take the first fenced code block that follows it.

    Args:
        text: Program file content.

    Returns:
        Mapping of lowercased heading text to the lines inside its first fenced block.
    """
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    inside = False
    lines: list[str] = []
    for line in text.splitlines():
        if inside:
            if _FENCE.match(line):
                blocks[current] = lines  # type: ignore[index]
                inside, current, lines = False, None, []
            else:
                lines.append(line)
            continue
        heading = _HEADING.match(line)
        if heading:
            current = heading.group(1).strip().lower()
            continue
        if current is not None and current not in blocks and _FENCE.match(line):
            inside, lines = True, []
    return blocks


def _parse_kv(lines: list[str]) -> dict[str, list[str]]:
    """Parse ``key: value`` lines with indented ``- value`` items into lists of values.

    Args:
        lines: Lines of one fenced block.

    Returns:
        Mapping of key to its values; a scalar value becomes a one-item list, an empty
        scalar followed by ``- item`` lines collects those items.
    """
    parsed: dict[str, list[str]] = {}
    key: str | None = None
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and key is not None and raw[:1].isspace():
            parsed[key].append(stripped[2:].strip().strip('"'))
            continue
        if ":" in stripped:
            key, value = (part.strip() for part in stripped.split(":", 1))
            parsed[key] = [value.strip('"')] if value else []
    return parsed


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a path glob where ``**`` spans directories and ``*``/``?`` stay inside one.

    Args:
        pattern: Glob such as ``src/**/*.py`` or ``pkg/``; a trailing ``/`` matches
            everything below that directory.

    Returns:
        Compiled regex anchored at both ends.
    """
    pattern = pattern.strip().removeprefix("./")
    if pattern.endswith("/"):
        pattern += "**"
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _in_scope(path: str, scope: list[str], root: Path) -> bool:
    """Return whether ``path`` matches a scope glob or lies under a scope directory."""
    for entry in scope:
        if _glob_regex(entry).match(path):
            return True
        if (root / entry).is_dir() and path.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def _evaluator_files(commands: list[str], root: Path) -> set[str]:
    """Return command tokens that name an existing file, as repo-relative POSIX paths."""
    found: set[str] = set()
    for command in commands:
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        for token in tokens:
            candidate = token.split("=", 1)[-1] if token.startswith("--") else token
            if candidate and (root / candidate).is_file():
                found.add(Path(candidate).as_posix().removeprefix("./"))
    return found


def changed_paths(root: Path) -> tuple[list[str], list[str]]:
    """Return ``(changed, untracked)`` repo-relative paths from ``git status --porcelain``.

    Untracked directories are expanded to files (``--untracked-files=all``); renames
    contribute both the old and the new path.

    Raises:
        subprocess.CalledProcessError: git failed.
    """
    git = shutil.which("git")
    if git is None:
        raise OSError("git not found on PATH")
    out = subprocess.run(  # noqa: S603 - fixed argv, git resolved via shutil.which
        [git, "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    changed: list[str] = []
    untracked: list[str] = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        paths = [path]
        if code[0] in "RC" and i < len(entries):
            paths.append(entries[i])
            i += 1
        for item in paths:
            if item.startswith(_EXCLUDED_PREFIXES):
                continue
            changed.append(item)
            if code == "??":
                untracked.append(item)
    return changed, untracked


def main(argv: list[str] | None = None) -> int:
    """Run the scope check and print the JSON result line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-json", required=True, help="research:run state.json of the active run")
    parser.add_argument("--root", default=".", help="repository root (default: cwd)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        state = json.loads(Path(args.state_json).read_text(encoding="utf-8"))
        changed, untracked = changed_paths(root)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"! BLOCKED — scope check could not read state or git status: {exc}", file=sys.stderr)
        return 1
    program = state.get("program_file")
    if not program or not Path(program).is_file():
        print(json.dumps({"changed": changed, "untracked": untracked, "violations": [], "checked": False}))
        return 0
    blocks = _section_blocks(Path(program).read_text(encoding="utf-8"))
    scope = _parse_kv(blocks.get("config", [])).get("scope_files", [])
    commands = [
        value for section in ("metric", "guard") for value in _parse_kv(blocks.get(section, [])).get("command", [])
    ]
    evaluators = _evaluator_files(commands, root)
    violations = [path for path in changed if path in evaluators or (scope and not _in_scope(path, scope, root))]
    print(json.dumps({"changed": changed, "untracked": untracked, "violations": violations, "checked": True}))
    return 3 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
