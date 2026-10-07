#!/usr/bin/env python
"""resolve_test_plan.py — pick the targeted tests for a change, and find the repository's own full-suite command.

``oss:resolve`` used to let every implementation and QA agent run the whole test suite after each fix — measured at
~10 minutes per run on a large repository, several times per resolve run. The contract now is: targeted tests inside
the fix loops, the full suite at the final gate with the repository's own command (rerun only after a fix). This
script supplies both halves deterministically, so no agent improvises either.

Subcommands:
    targeted --base REF        tests impacted by the files changed since REF (plus staged/unstaged/untracked work)
    targeted --files F [F ...] tests impacted by an explicit file list
    full-command [--script P]  the repository's documented full-suite command and where it was found; with
                               ``--script`` also write it to a shell script, so the gate runs one fixed command

Targeted selection uses ``codemap-py query test-impact <module>`` per changed module when the CLI and its index are
usable, and falls back per module to path heuristics (``test_<name>.py`` / ``<name>_test.py`` files, and test files
that import the module). A codemap failure is never read as "no affected tests" — that module falls back instead.

Output: one JSON object on stdout.

Exit codes:
    0 — plan printed (an empty ``tests`` list is a valid result and says so in ``note``)
    1 — not inside a git work tree, or a ref/file argument starts with '-' (argv-injection guard)
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

#: Matches Python test file names, ``test_*.py`` or ``*_test.py``.
_TEST_NAME = re.compile(r"(^test_.*|.*_test)\.py$")
#: Contributor documentation files scanned for the project's documented test commands.
_DOC_SOURCES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".github/CONTRIBUTING.md")
#: Matches inline Markdown code spans, capturing the text between the backticks.
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
#: Recognizes a test runner invocation (pytest, make test/check, tox, nox) in a documented command.
_RUNNER_HINT = re.compile(r"\b(pytest|make (?:test|check)|tox|nox)\b")
#: Layout prefixes dropped from a file path when deriving its dotted module name.
_SOURCE_ROOTS = ("src/", "lib/")


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


def changed_files(repo: Path, base: str) -> list[str]:
    """List Python files changed since ``base``, including staged, unstaged and untracked work.

    Args:
        repo: Repository root.
        base: Git revision the change set starts from.

    Returns:
        Sorted unique POSIX paths of changed ``.py`` files that still exist.
    """
    _, committed = _run(["git", "diff", "--name-only", f"{base}...HEAD", "--"], repo)
    _, working = _run(["git", "diff", "--name-only", "HEAD", "--"], repo)
    _, untracked = _run(["git", "ls-files", "--others", "--exclude-standard"], repo)
    names = {line.strip() for line in (committed + "\n" + working + "\n" + untracked).splitlines() if line.strip()}
    return sorted(name for name in names if name.endswith(".py") and (repo / name).is_file())


def codemap_tests(module: str, repo: Path) -> list[str] | None:
    """Ask codemap which test files a module change impacts.

    Args:
        module: Dotted module name.
        repo: Repository root.

    Returns:
        Test file paths, or ``None`` when codemap is unavailable, fails, or reports an incomplete or stale answer —
        never an empty list standing in for a failure.
    """
    if shutil.which("codemap-py") is None:
        return None
    code, out = _run(["codemap-py", "query", "test-impact", module], repo, timeout=120)
    if code != 0:
        return None
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not index_trusted(payload):
        return None
    files = payload.get("test_files")
    return [str(item) for item in files] if isinstance(files, list) else None


def index_trusted(payload: dict[str, object]) -> bool:
    """Tell whether a test-impact payload's index block vouches for a complete, current answer.

    Codemap nests its completeness facts under ``payload["index"]``; a missing block, a stale or undetermined index, an
    incomplete query, or degraded (unparsed) modules all make the test list untrustworthy.

    Args:
        payload: Parsed ``codemap-py query test-impact`` output.

    Returns:
        ``True`` only for a fresh, complete, non-degraded index.

    Examples:
        >>> index_trusted({"index": {"stale": False, "query_complete": True, "degraded": 0}})
        True
        >>> index_trusted({"index": {"stale": True}}), index_trusted({"index": {"query_complete": False}})
        (False, False)
        >>> index_trusted({"index": {"degraded": 2}}), index_trusted({"test_files": []})
        (False, False)
    """
    index = payload.get("index")
    if not isinstance(index, dict):
        return False
    return not (
        index.get("stale")
        or index.get("stale_undetermined")
        or index.get("query_complete") is False
        or index.get("degraded")
    )


def heuristic_tests(path: str, all_tests: list[str], repo: Path) -> list[str]:
    """Find test files for a source file by name convention and by import.

    Args:
        path: Changed source file, POSIX path relative to the repository root.
        all_tests: Every test file in the repository.
        repo: Repository root.

    Returns:
        Test files named after the module, plus test files that import it.
    """
    stem = PurePosixPath(path).stem
    dotted = module_name(path)
    leaf = dotted.rsplit(".", 1)[-1] if dotted else stem
    named = {f"test_{stem}.py", f"{stem}_test.py"}
    importer = re.compile(
        rf"^\s*(from\s+{re.escape(dotted)}\b|import\s+{re.escape(dotted)}\b|from\s+\S+\s+import\s+.*\b{re.escape(leaf)}\b)",
        re.M,
    )
    hits = []
    for test in all_tests:
        if PurePosixPath(test).name in named:
            hits.append(test)
            continue
        try:
            text = (repo / test).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if dotted and importer.search(text):
            hits.append(test)
    return hits


def plan_targeted(repo: Path, files: list[str]) -> dict[str, object]:
    """Build the targeted test plan for a set of changed files.

    Args:
        repo: Repository root.
        files: Changed files, POSIX paths relative to the repository root.

    Returns:
        ``changed``, ``tests``, ``sources`` (test → codemap/heuristic/changed), ``codemap`` usage and ``note``.
    """
    _, listed = _run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"], repo)
    all_tests = sorted(line for line in listed.splitlines() if is_test_file(line))
    sources: dict[str, str] = {}
    codemap_used = codemap_failed = 0
    for path in files:
        if is_test_file(path):
            sources.setdefault(path, "changed")
            continue
        found = codemap_tests(module_name(path), repo) if path.endswith(".py") else None
        if found is None:
            codemap_failed += shutil.which("codemap-py") is not None
            found, origin = heuristic_tests(path, all_tests, repo), "heuristic"
        else:
            codemap_used += 1
            origin = "codemap"
        for test in found:
            sources.setdefault(test, origin)
    tests = sorted(test for test in sources if (repo / test).is_file())
    note = "" if tests else "no targeted tests found for this change — say so; never substitute the full suite here"
    return {
        "changed": files,
        "tests": tests,
        "sources": {test: sources[test] for test in tests},
        "codemap": {"used": codemap_used, "fell_back": codemap_failed},
        "note": note,
    }


def _commands_in(text: str) -> list[tuple[int, str]]:
    """Extract runnable test commands from Markdown: fenced lines and inline code spans.

    Args:
        text: Markdown document.

    Returns:
        ``(line_number, command)`` pairs mentioning a test runner, without placeholder text.
    """
    found: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        candidates = [line.strip().lstrip("$ ").strip()] if in_fence else _CODE_SPAN.findall(line)
        for candidate in candidates:
            command = candidate.split(" #", 1)[0].strip()
            if _RUNNER_HINT.search(command) and "<" not in command and not command.startswith("#"):
                found.append((number, command))
    return found


def _makefile_target(repo: Path) -> str | None:
    """Return ``make test`` when the Makefile defines a ``test`` target.

    Args:
        repo: Repository root.

    Returns:
        ``"make test"`` or ``None``.
    """
    makefile = repo / "Makefile"
    if makefile.is_file() and re.search(r"^test\s*:", makefile.read_text(encoding="utf-8", errors="replace"), re.M):
        return "make test"
    return None


def full_command(repo: Path) -> dict[str, object]:
    """Find the repository's own full-suite command, preferring what its contributor docs prescribe.

    Args:
        repo: Repository root.

    Returns:
        ``command``, ``source`` (``file:line`` or a fallback label) and ``runner`` — the command prefix to reuse for
        targeted runs, with any path arguments removed.
    """
    for name in _DOC_SOURCES:
        doc = repo / name
        if not doc.is_file():
            continue
        commands = _commands_in(doc.read_text(encoding="utf-8", errors="replace"))
        pytest_commands = [item for item in commands if "pytest" in item[1]]
        if pytest_commands or commands:
            number, command = (pytest_commands or commands)[0]
            return {"command": command, "source": f"{name}:{number}", "runner": runner_prefix(command, repo)}
    make = _makefile_target(repo)
    if make:
        return {"command": make, "source": "Makefile:test", "runner": "python -m pytest"}
    return {"command": "python -m pytest", "source": "fallback (no documented command)", "runner": "python -m pytest"}


def runner_prefix(command: str, repo: Path) -> str:
    """Strip path arguments from a pytest command so targeted test paths can be appended to it.

    Args:
        command: Full-suite command line.
        repo: Repository root, to recognise which arguments are paths.

    Returns:
        The command without arguments naming an existing file or directory; non-pytest commands map to
        ``python -m pytest``.

    Examples:
        >>> runner_prefix("make test", Path("."))
        'python -m pytest'
    """
    if "pytest" not in command:
        return "python -m pytest"
    tokens = shlex.split(command)
    kept = [
        token
        for token in tokens
        if token.startswith("-") or not (repo / token.split("::")[0]).exists() or token in {"python", "uv", "run"}
    ]
    return shlex.join(kept)


def _repo_root() -> Path | None:
    """Return the enclosing git work tree root, or ``None`` outside one."""
    code, out = _run(["git", "rev-parse", "--show-toplevel"], Path.cwd())
    return Path(out.strip()) if code == 0 and out.strip() else None


def main(argv: list[str] | None = None) -> int:
    """Print the requested plan as one JSON line.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        Process exit code (see module docstring).
    """
    parser = argparse.ArgumentParser(description="Targeted tests and the repository's full-suite command.")
    sub = parser.add_subparsers(dest="mode", required=True)
    targeted = sub.add_parser("targeted", help="tests impacted by a change")
    group = targeted.add_mutually_exclusive_group(required=True)
    group.add_argument("--base", help="git revision the change set starts from")
    group.add_argument("--files", nargs="+", help="explicit changed files, relative to the repository root")
    full = sub.add_parser("full-command", help="the repository's documented full-suite command")
    full.add_argument("--script", help="also write the command to this shell script path")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    repo = _repo_root()
    if repo is None:
        print(json.dumps({"error": "not inside a git work tree"}))
        return 1
    if args.mode == "full-command":
        found = full_command(repo)
        if args.script:
            script = Path(args.script)
            script.write_text(
                f"#!/usr/bin/env bash\ncd {shlex.quote(repo.as_posix())}\n{found['command']}\n",
                encoding="utf-8",
                newline="\n",
            )
            found["script"] = script.as_posix()
        print(json.dumps(found))
        return 0
    values = [args.base] if args.base else list(args.files)
    if any(value.startswith("-") for value in values):
        print(json.dumps({"error": "a ref or file argument starts with '-'"}))
        return 1
    files = changed_files(repo, args.base) if args.base else sorted({PurePosixPath(f).as_posix() for f in args.files})
    plan = plan_targeted(repo, files)
    runner = full_command(repo)["runner"]
    plan["command"] = f"{runner} {shlex.join(plan['tests'])}" if plan["tests"] else None  # type: ignore[arg-type]
    print(json.dumps(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
