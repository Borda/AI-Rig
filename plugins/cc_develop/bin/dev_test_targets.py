#!/usr/bin/env python
"""dev_test_targets.py — pick the tests a change actually touches, so fix and refactor loops never rerun the full suite.

``/develop:fix`` and ``/develop:refactor`` used to fall back to the whole test directory whenever ``codemap-py`` was
absent or returned nothing, and their review loops re-ran the full suite every cycle — measured at up to seven full
runs in one refactor session. The contract now is: targeted tests inside the loops, the full suite at the
final gate with the repository's own command. This script supplies the targeted half deterministically, so no loop
improvises its own selection or silently widens to the full suite.

Selection per changed source module:

1. ``codemap-py query test-impact <module>`` when the CLI is installed and answers. A complete, fresh answer is used as
   is. A stale or incomplete answer keeps its hits and adds the heuristic hits below, because a stale index can miss
   tests in either direction.
2. Otherwise path heuristics: ``test_<name>.py`` / ``<name>_test.py`` files, plus test files that import the module.

A codemap failure is never read as "no affected tests" — that module falls back instead. Changed test files are always
selected themselves.

Usage:
    python dev_test_targets.py --pytest-cmd "$PYTEST_CMD"                 # changes vs HEAD, incl. untracked
    python dev_test_targets.py --pytest-cmd "$PYTEST_CMD" --base <ref>    # plus commits since <ref>
    python dev_test_targets.py --pytest-cmd "$PYTEST_CMD" --files a.py b.py
    python dev_test_targets.py --pytest-cmd "$PYTEST_CMD" --run           # select, then run in the same call
    python dev_test_targets.py --pytest-cmd "$PYTEST_CMD" --run --record-dir "$DEV_DIR"   # plus a run record

Output: one JSON object on stdout — ``changed``, ``tests``, ``sources`` (test → codemap/heuristic/changed),
``codemap`` usage counts, ``command`` (``$PYTEST_CMD`` plus the selected tests, or ``null``) and ``note``. With
``--run`` the selection then runs through ``run_pytest_short.py`` (same runner allowlist, ``--tb=short``, last 20
lines), so a loop cycle costs one tool call instead of a select call plus a run call.

``--record-dir DIR`` keeps every selection inspectable after the run: each call appends one JSON line to
``DIR/test-targets.jsonl`` (UTC time, changed files, selected tests, why each was selected — codemap, heuristic, or
changed — codemap usage and fallbacks, codemap's ``not_covered`` caveats, exit code, log path), and with ``--run``
the full runner output goes to ``DIR/test-targets-<UTC time>.log`` while the terminal shows its last 40 lines.

Exit codes:
    0 — plan printed (an empty ``tests`` list is a valid result and says so in ``note``); with ``--run``, the selected
        tests passed, or nothing was selected so nothing ran
    1 — not inside a git work tree, ``--pytest-cmd`` empty, or a ref/file argument starts with '-'
    other — with ``--run``, pytest's own exit code, or 2 when ``--pytest-cmd`` is not an allowlisted runner
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

#: Matches a Python test file name: test_*.py or *_test.py.
_TEST_NAME = re.compile(r"(^test_.*|.*_test)\.py$")
#: Layout prefixes dropped from a path when turning it into a dotted module name.
_SOURCE_ROOTS = ("src/", "lib/")
#: Guidance returned when no targeted tests were found: run only the tests the step names, not the full suite.
_EMPTY_NOTE = (
    "no targeted tests found for this change — run only the tests this step already names (regression or"
    " characterization); the full suite runs at the final gate, never here"
)


@dataclass
class CodemapAnswer:
    """One module's codemap test-impact result and whether it can stand alone."""

    tests: list[str]
    trusted: bool
    not_covered: object = None


@dataclass
class TargetPlan:
    """Targeted test selection for one change set."""

    changed: list[str]
    sources: dict[str, str] = field(default_factory=dict)
    codemap_used: int = 0
    codemap_partial: int = 0
    codemap_fell_back: int = 0
    not_covered: dict[str, object] = field(default_factory=dict)


def _run(args: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
    """Run one read-only command and return its exit code and stdout.

    Args:
        args: Command and arguments.
        cwd: Working directory.
        timeout: Maximum wait in seconds.

    Returns:
        ``(returncode, stdout)``; the code is 1 and stdout empty when the command cannot run at all.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False, encoding="utf-8"
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout


def is_test_file(path: str) -> bool:
    """Tell whether a repository path is a pytest test module.

    Args:
        path: POSIX-style path relative to the repository root.

    Returns:
        ``True`` for ``test_*.py`` and ``*_test.py`` files.

    Examples:
        >>> is_test_file("tests/unit/test_api.py"), is_test_file("pkg/api_test.py"), is_test_file("pkg/api.py")
        (True, True, False)
    """
    return bool(_TEST_NAME.match(PurePosixPath(path).name))


def module_name(path: str) -> str:
    """Turn a source file path into the dotted module name codemap indexes it under.

    Args:
        path: POSIX-style ``.py`` path relative to the repository root.

    Returns:
        Dotted module name; a ``src/``/``lib/`` layout prefix is dropped and ``__init__`` names the package.

    Examples:
        >>> module_name("src/pkg/core/api.py"), module_name("pkg/__init__.py")
        ('pkg.core.api', 'pkg')
    """
    for root in _SOURCE_ROOTS:
        if path.startswith(root):
            path = path[len(root) :]
            break
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def changed_files(repo: Path, base: str | None) -> list[str]:
    """List Python files changed against HEAD (and since ``base`` when given), including untracked work.

    Args:
        repo: Repository root.
        base: Optional git revision whose later commits also count as changed.

    Returns:
        Sorted unique POSIX paths of changed ``.py`` files that still exist.
    """
    outputs = [_run(["git", "diff", "--name-only", "HEAD", "--"], repo)[1]]
    outputs.append(_run(["git", "ls-files", "--others", "--exclude-standard"], repo)[1])
    if base:
        outputs.append(_run(["git", "diff", "--name-only", f"{base}...HEAD", "--"], repo)[1])
    names = {line.strip() for out in outputs for line in out.splitlines() if line.strip()}
    return sorted(name for name in names if name.endswith(".py") and (repo / name).is_file())


def parse_codemap(stdout: str) -> CodemapAnswer | None:
    """Read one ``codemap-py query test-impact`` JSON answer.

    The honesty signals live in the nested ``index`` coverage block, not at top level. A missing block, a stale or
    undetermined index, an incomplete query, or degraded (unparsed) modules all make the answer untrusted — the same
    rule ``oss:resolve``'s selector applies.

    Args:
        stdout: Raw command output.

    Returns:
        The answer, or ``None`` when the output is not a usable test-impact payload.

    Examples:
        >>> parse_codemap('{"test_files": ["tests/test_a.py"], "index": {"query_complete": true, "stale": []}}')
        CodemapAnswer(tests=['tests/test_a.py'], trusted=True, not_covered=None)
        >>> parse_codemap('{"test_files": [], "index": {"query_complete": false}}').trusted
        False
        >>> parse_codemap("not json") is None
        True
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("test_files"), list):
        return None
    index = payload.get("index") if isinstance(payload.get("index"), dict) else {}
    trusted = index.get("query_complete") is True and not (
        index.get("stale") or index.get("stale_undetermined") or index.get("degraded")
    )
    tests = [str(item) for item in payload["test_files"]]
    return CodemapAnswer(tests=tests, trusted=trusted, not_covered=index.get("not_covered") or None)


def codemap_tests(module: str, repo: Path) -> CodemapAnswer | None:
    """Ask codemap which test files a module change impacts.

    Args:
        module: Dotted module name.
        repo: Repository root.

    Returns:
        The parsed answer, or ``None`` when codemap is not installed, fails, or prints no usable payload.
    """
    if shutil.which("codemap-py") is None:
        return None
    code, out = _run(["codemap-py", "query", "test-impact", module], repo, timeout=120)
    return parse_codemap(out) if code == 0 else None


class SuiteFiles:
    """Every test file in the repository, with contents read at most once."""

    def __init__(self, repo: Path) -> None:
        """List the repository's test files.

        Args:
            repo: Repository root.
        """
        self._repo = repo
        listed = _run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"], repo)[1]
        self.files = sorted(line for line in listed.splitlines() if is_test_file(line))
        self._texts: dict[str, str] = {}

    def text(self, test: str) -> str:
        """Return one test file's contents, empty when unreadable."""
        if test not in self._texts:
            try:
                self._texts[test] = (self._repo / test).read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._texts[test] = ""
        return self._texts[test]


def heuristic_tests(path: str, corpus: SuiteFiles) -> list[str]:
    """Find test files for a source file by name convention and by import.

    Args:
        path: Changed source file, POSIX path relative to the repository root.
        corpus: The repository's test files.

    Returns:
        Test files named after the module, plus test files that import it.
    """
    stem = PurePosixPath(path).stem
    dotted = module_name(path)
    leaf = dotted.rsplit(".", 1)[-1] if dotted else stem
    named = {f"test_{stem}.py", f"{stem}_test.py"}
    importer = re.compile(
        rf"^\s*(from\s+{re.escape(dotted)}\b|import\s+{re.escape(dotted)}\b|from\s+\S+\s+import\s+.*\b{re.escape(leaf)}\b)",
        re.MULTILINE,
    )
    return [
        test
        for test in corpus.files
        if PurePosixPath(test).name in named or (dotted and importer.search(corpus.text(test)))
    ]


def _select_for_source(path: str, repo: Path, corpus: SuiteFiles, plan: TargetPlan) -> None:
    """Add the tests one changed source file impacts to the plan, recording where each came from."""
    answer = codemap_tests(module_name(path), repo)
    if answer is not None and answer.not_covered:
        plan.not_covered[path] = answer.not_covered
    if answer is not None:
        for test in answer.tests:
            plan.sources.setdefault(test, "codemap")
        if answer.trusted:
            plan.codemap_used += 1
            return
        plan.codemap_partial += 1
    elif shutil.which("codemap-py") is not None:
        plan.codemap_fell_back += 1
    for test in heuristic_tests(path, corpus):
        plan.sources.setdefault(test, "heuristic")


def plan_targeted(repo: Path, files: list[str]) -> TargetPlan:
    """Build the targeted test plan for a set of changed files.

    Args:
        repo: Repository root.
        files: Changed files, POSIX paths relative to the repository root.

    Returns:
        The plan; tests that no longer exist on disk are dropped.
    """
    corpus = SuiteFiles(repo)
    plan = TargetPlan(changed=files)
    for path in files:
        if is_test_file(path):
            plan.sources.setdefault(path, "changed")
        elif path.endswith(".py"):
            _select_for_source(path, repo, corpus, plan)
    plan.sources = {test: origin for test, origin in sorted(plan.sources.items()) if (repo / test).is_file()}
    return plan


def render(plan: TargetPlan, pytest_cmd: str) -> dict[str, object]:
    """Turn a plan into the JSON payload the skills read.

    Args:
        plan: Targeted selection.
        pytest_cmd: The repository's pytest invocation from runner detection.

    Returns:
        JSON-ready dict; ``command`` is ``None`` when nothing was selected.
    """
    tests = list(plan.sources)
    return {
        "changed": plan.changed,
        "tests": tests,
        "sources": plan.sources,
        "codemap": {"used": plan.codemap_used, "partial": plan.codemap_partial, "fell_back": plan.codemap_fell_back},
        "not_covered": plan.not_covered,
        "command": f"{pytest_cmd} {shlex.join(tests)}" if tests else None,
        "note": "" if tests else _EMPTY_NOTE,
    }


def record(record_dir: Path, payload: dict[str, object], exit_code: int | None, log: Path | None) -> None:
    """Append one selection record to ``record_dir/test-targets.jsonl``.

    Args:
        record_dir: Run artifact directory (the skill's ``$DEV_DIR``).
        payload: The rendered selection.
        exit_code: Runner exit code, or ``None`` when nothing ran.
        log: Full runner output file, or ``None`` when nothing ran.
    """
    record_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **payload,
        "exit": exit_code,
        "log": log.as_posix() if log else None,
    }
    with (record_dir / "test-targets.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry) + "\n")


def _repo_root() -> Path | None:
    """Return the enclosing git work tree root, or ``None`` outside one."""
    code, out = _run(["git", "rev-parse", "--show-toplevel"], Path.cwd())
    return Path(out.strip()) if code == 0 and out.strip() else None


def _error(message: str) -> int:
    """Print one JSON error line and return the failure exit code."""
    print(json.dumps({"error": message}))
    return 1


def main(argv: list[str] | None = None) -> int:
    """Print the targeted test plan as one JSON line.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        Process exit code (see module docstring).
    """
    parser = argparse.ArgumentParser(description="Targeted tests for the current change.")
    parser.add_argument("--pytest-cmd", required=True, help="repository pytest invocation ($PYTEST_CMD)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--base", help="git revision whose later commits also count as changed")
    group.add_argument("--files", nargs="+", help="explicit changed files, relative to the repository root")
    parser.add_argument("--run", action="store_true", help="run the selected tests after printing the plan")
    parser.add_argument("--record-dir", help="append a selection record (and keep the full run log) in this dir")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    if not args.pytest_cmd.strip():
        return _error("--pytest-cmd is empty; re-read the dev-pytest-cmd sentinel from runner detection")
    values = [args.base] if args.base else list(args.files or [])
    if any(value.startswith("-") for value in values):
        return _error("a ref or file argument starts with '-'")
    repo = _repo_root()
    if repo is None:
        return _error("not inside a git work tree")
    files = sorted({Path(f).as_posix() for f in args.files}) if args.files else changed_files(repo, args.base)
    payload = render(plan_targeted(repo, files), args.pytest_cmd.strip())
    record_dir = Path(args.record_dir) if args.record_dir else None
    log = record_dir / f"test-targets-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.log" if record_dir else None
    if log:
        payload["log"] = log.as_posix() if args.run and payload["tests"] else None
    print(json.dumps(payload), flush=True)
    if not args.run or not payload["tests"]:
        if record_dir:
            record(record_dir, payload, None, None)
        return 0
    import run_pytest_short  # sibling bin/ script; imported lazily so plan-only calls never load it

    runner_args = [args.pytest_cmd.strip(), *payload["tests"]]
    if log:
        runner_args += ["--tail-n", "40", "--log", str(log)]
    code = run_pytest_short.main(runner_args)
    if record_dir:
        record(record_dir, payload, code, log)
    return code


if __name__ == "__main__":
    sys.exit(main())
