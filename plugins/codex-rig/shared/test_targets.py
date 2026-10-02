#!/usr/bin/env python3
"""Choose targeted pytest files for changed source and decide whether the sandbox-safe flag applies.

## Purpose

Fix, challenge, reproduction, and review loops need the tests that exercise the changed files, not the whole suite. This
helper turns one change set into a deterministic pytest target list so loops run narrow, sandbox-safe checks and the
full suite runs once at the canonical gate.

## Scope

It reads the Git change set or explicit paths, asks codemap-py ``diff-impact`` for the unioned test impact when a
launcher is available, and otherwise falls back to name and import heuristics. It inspects pytest configuration only to
decide whether ``-p no:xdist`` may be added. It never runs tests, edits files, installs tools, or changes the canonical
gate command.

## Usage

``python test_targets.py --root <repository> [--base <ref>] [--changed <path> ...] [--provider-root <path>]``. Without
``--changed`` it uses ``git diff --name-only <base>`` plus untracked files. ``--no-codemap`` forces the heuristics.

## Used by

Code Remediate, Implement, Investigate, Challenge Resolve, and Code Review apply it under the native contract's
Sandboxed Test Runs section.

## Outputs

One JSON object: ``targets`` (sorted repository-relative test files), ``sources`` (how each target was chosen),
``unmapped`` (changed Python sources with no target), ``config_changed`` (test configuration files that changed),
``codemap`` status, ``sandbox_safe`` with its reason, and ``pytest_args`` ready to append to the pinned test runner.
``--out`` also writes the same report into the run directory so a targeted run stays auditable.

## Failure

A Git error or unreadable root exits ``2`` with a stable code. Codemap absence, failure, or a stale index falls back to
heuristics and is reported in ``codemap``; it is never fatal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

SHARED_DIRECTORY = Path(__file__).resolve().parent
if str(SHARED_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SHARED_DIRECTORY))

import codemap_adapter  # noqa: E402

CONFIG_NAMES = {"conftest.py", "pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "noxfile.py", "setup.py"}
PARALLEL_OPTION = re.compile(r"(?:^|[\s\"'\[,])(?:-n|--numprocesses|--dist|-p\s*xdist)(?:[=\s\"',\]]|$)")
XDIST_FIXTURES = re.compile(r"\b(?:worker_id|testrun_uid)\b")
SKIPPED_DIRECTORIES = {".git", ".venv", "venv", "node_modules", ".tox", "__pycache__", ".reports"}


class TargetError(Exception):
    """Report an input problem that prevents choosing targets."""


def _git(root: Path, *arguments: str) -> list[str]:
    """Return the non-empty output lines of one read-only Git command."""
    completed = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise TargetError(f"git-failed:{arguments[0]}")
    return [line for line in completed.stdout.splitlines() if line.strip()]


def changed_files(root: Path, base: str) -> list[str]:
    """List tracked changes against ``base`` plus untracked files, as POSIX repository-relative paths."""
    tracked = _git(root, "diff", "--name-only", base)
    untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    return sorted({PurePosixPath(path).as_posix() for path in (*tracked, *untracked)})


def is_test_file(path: str) -> bool:
    """Recognize a pytest test module by its file name.

    Example:
        >>> is_test_file("tests/unit/test_api.py"), is_test_file("src/api_test.py"), is_test_file("src/api.py")
        (True, True, False)
    """
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _python_test_layout(root: Path) -> tuple[list[str], list[str]]:
    """List test modules and conftest files under the root, skipping environments and generated trees."""
    tests, conftests = [], []
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in SKIPPED_DIRECTORIES]
        for name in files:
            relative = Path(directory, name).relative_to(root).as_posix()
            if is_test_file(relative):
                tests.append(relative)
            elif name == "conftest.py":
                conftests.append(relative)
    return sorted(tests), sorted(conftests)


def _module_name(path: str) -> str:
    """Return the dotted module name for a source path, dropping a leading ``src`` layout directory."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def heuristic_targets(root: Path, source: str, tests: list[str]) -> list[str]:
    """Pick tests named after the changed module or importing it."""
    stem = PurePosixPath(source).stem
    module = _module_name(source)
    named = {f"test_{stem}.py", f"{stem}_test.py"}
    chosen = [test for test in tests if PurePosixPath(test).name in named]
    if module:
        pattern = re.compile(rf"(?:^|\s)(?:from|import)\s+{re.escape(module)}\b", re.MULTILINE)
        for test in tests:
            if test in chosen:
                continue
            try:
                if pattern.search((root / test).read_text(encoding="utf-8", errors="replace")):
                    chosen.append(test)
            except OSError:
                continue
    return chosen


def codemap_targets(root: Path, base: str, provider_root: Path | None, timeout: float) -> tuple[list[str] | None, str]:
    """Ask codemap-py for the unioned test impact of the change set; return ``None`` when unusable."""
    resolution = codemap_adapter._resolve_codemap_executable(provider_root)
    if resolution.launcher is None:
        return None, resolution.status
    argv = [resolution.launcher, "query", "--compact", "--root", str(root), "diff-impact", "--base", base]
    exit_code, payload, error = codemap_adapter._run_json(argv, timeout)
    if exit_code != 0 or not isinstance(payload, dict):
        return None, f"failed:{error or exit_code}"
    # Completeness metadata is nested under ``index`` (codemap-py coverage block), never at the payload top level.
    index = payload.get("index")
    if not isinstance(index, dict) or index.get("stale", True):
        return None, "stale-index"
    impact = payload.get("test_impact") or {}
    files = impact.get("test_files") if isinstance(impact, dict) else None
    if not isinstance(files, list):
        return None, "unsupported-output"
    status = "available" if index.get("query_complete", index.get("exhaustive", False)) else "incomplete"
    return sorted(PurePosixPath(str(path)).as_posix() for path in files), status


def sandbox_safe(root: Path, targets: list[str], conftests: list[str]) -> tuple[bool, str]:
    """Decide whether ``-p no:xdist`` may be added under the Sandboxed Test Runs conditions."""
    if PARALLEL_OPTION.search(os.environ.get("PYTEST_ADDOPTS", "")):
        return False, "PYTEST_ADDOPTS requests parallelism"
    for name in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"):
        path = root / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            # Conservative: any parallel token in a file that sets addopts withholds the flag, covering list forms.
            if "addopts" in text and PARALLEL_OPTION.search(text):
                return False, f"{name} addopts requests parallelism"
    for relative in [*targets, *conftests]:
        path = root / relative
        if path.is_file() and XDIST_FIXTURES.search(path.read_text(encoding="utf-8", errors="replace")):
            return False, f"{PurePosixPath(relative).as_posix()} uses xdist fixtures"
    return True, "no parallel option or xdist fixture found"


def choose(root: Path, changed: list[str], base: str, use_codemap: bool, provider_root: Path | None) -> dict[str, Any]:
    """Build the full target report for one change set."""
    sources: dict[str, str] = {}
    for path in changed:
        if is_test_file(path) and (root / path).is_file():
            sources[path] = "changed-test"
    config_changed = [path for path in changed if PurePosixPath(path).name in CONFIG_NAMES]
    python_sources = [
        path for path in changed if path.endswith(".py") and not is_test_file(path) and path not in config_changed
    ]
    impact, codemap_status = codemap_targets(root, base, provider_root, 30.0) if use_codemap else (None, "disabled")
    unmapped = []
    tests, conftests = _python_test_layout(root)
    for test in impact or []:
        sources.setdefault(test, "codemap")
    if impact is None or codemap_status == "incomplete":
        # An incomplete index may miss tests, so the heuristics add theirs instead of being skipped.
        for source in python_sources:
            matches = heuristic_targets(root, source, tests)
            for test in matches:
                sources.setdefault(test, "heuristic")
            if not matches:
                unmapped.append(source)
    targets = sorted(path for path in sources if (root / path).is_file())
    safe, reason = sandbox_safe(root, targets, conftests)
    return {
        "targets": targets,
        "sources": {path: sources[path] for path in targets},
        "unmapped": unmapped,
        "config_changed": config_changed,
        "codemap": codemap_status,
        "sandbox_safe": safe,
        "sandbox_reason": reason,
        "pytest_args": [*targets, *(["-p", "no:xdist"] if safe and targets else [])],
    }


def main(argv: list[str] | None = None) -> int:
    """Print the target report for the requested change set."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root.")
    parser.add_argument("--base", default="HEAD", help="Git ref the change set is compared against.")
    parser.add_argument("--changed", action="append", default=[], help="Explicit changed path; repeatable.")
    parser.add_argument("--provider-root", type=Path, help="Absolute active codemap-py install root.")
    parser.add_argument("--no-codemap", action="store_true", help="Use name and import heuristics only.")
    parser.add_argument("--out", type=Path, help="Also write the JSON report to this run-directory path.")
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    try:
        if not root.is_dir():
            raise TargetError("root-not-directory")
        changed = [PurePosixPath(path).as_posix() for path in arguments.changed] or changed_files(root, arguments.base)
        report = choose(root, changed, arguments.base, not arguments.no_codemap, arguments.provider_root)
    except TargetError as error:
        print(f"test-targets-error:{error}", file=sys.stderr)
        return 2
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if arguments.out is not None:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(encoded + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
