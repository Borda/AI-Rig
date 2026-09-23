#!/usr/bin/env python
"""run_audit_checks.py — pre-release readiness data gathering for /oss:release audit.

Prints data sections separated by ``--- check: <name> ---`` banners so
the release agent can extract per-check output for the readiness table.
Interpretive steps (README alignment, CHANGELOG coverage judgement,
severity assignment) remain in templates/audit-checks.md — this script
emits raw evidence only.

Usage:
    run_audit_checks.py --repo <owner/repo> [--tag <version>] [--range <git-range>]

Defaults:
    range = $LAST_TAG..HEAD where LAST_TAG falls back to last stable tag
            or the initial commit when no tags exist.

Missing pip-audit:
    Stays non-interactive — emits ``PIP_AUDIT_MISSING_SIGNAL`` as its own
    output line instead of prompting. Caller (templates/audit-checks.md
    "Check 6 interpretation") greps for it and offers an install-and-rerun
    path via AskUserQuestion.

Exit codes:
    0 — all data-gathering checks ran (warnings on optional/missing tools)
    1 — bad args
    2 — gh CLI not authenticated or invalid tag (option-injection guard)
"""

from __future__ import annotations

import argparse
import itertools
import os
import re
import subprocess
import sys
from pathlib import Path
from shutil import which

_VERSION_RE: re.Pattern[str] = re.compile(r"__version__|^version\s*=", re.MULTILINE)
_SIGNALS_RE: re.Pattern[str] = re.compile(r"TODO.*release|FIXME|HACK|XXX")
_DOCS_RE: re.Pattern[str] = re.compile(r"readme|\.md$|docs/", re.IGNORECASE)
# Allowlist for LAST_TAG / discovered tag: either a SemVer-style version tag
# (optional leading ``v``, two or three numeric components, optional pre-release
# suffix) or a short SHA (7–40 hex chars).  Replaces the original deny-list
# (``startswith('-')``) which let arbitrary git ref expressions through (A03:2021).
_TAG_OR_SHA_RE: re.Pattern[str] = re.compile(r"^(?:v?[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:-[a-zA-Z0-9.]+)?|[0-9a-f]{7,40})$")
_EXCLUDE_TAG_FLAGS: tuple[str, ...] = (
    "--exclude=*rc*",
    "--exclude=*dev*",
    "--exclude=*alpha*",
    "--exclude=*beta*",
)
_MAX_VERSION_LINES = 15
_MAX_SIGNAL_LINES = 10
_MAX_SCAN_FILE_SIZE = 10 * 1024 * 1024  # 10 MB guard against runaway reads — matches sibling scripts
_MAX_SCAN_FILES = 20_000  # hard cap on rglob() traversal — matches sibling scripts' file-count discipline
# Machine-readable line the caller greps for to detect the missing-tool gap and
# offer an install-and-rerun path — this script stays non-interactive, so the
# banner is the only signal available to the skill-level AskUserQuestion gate
# (see templates/audit-checks.md "Check 6 interpretation").
PIP_AUDIT_MISSING_SIGNAL = "pip-audit-status: not-installed"
# Module-level, grep-able like PIP_AUDIT_MISSING_SIGNAL — this is a release-readiness audit, so a
# scan that silently stops early must announce it rather than reporting a false "nothing found".
SCAN_TRUNCATED_SIGNAL = "scan-truncated: file-count-cap-reached"


def _resolve(cmd: str) -> str:
    """Resolve a CLI tool to its absolute path.

    Args:
        cmd: Bare executable name (e.g. ``"git"``).

    Returns:
        Absolute path to the executable.

    Raises:
        FileNotFoundError: If ``cmd`` is not present on ``PATH``.

    Examples:
        >>> import shutil
        >>> _resolve("git") == shutil.which("git")
        True
    """
    p = which(cmd)
    if p is None:
        raise FileNotFoundError(f"executable not found on PATH: {cmd}")
    return p


def _run(cmd: list[str], *, timeout: int = 10, text: bool = True) -> str:
    """Run a command; return stdout string (empty on failure).

    Args:
        cmd: Command list to execute.
        timeout: Subprocess timeout in seconds.
        text: Whether to decode stdout as text.

    Returns:
        Stripped stdout on exit-0; empty string on failure.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    result = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=text, check=False, timeout=timeout
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _grep_version_files() -> list[str]:
    """Scan source files for ``__version__`` / ``version =`` patterns.

    Replicates: ``grep -rn '__version__|^version\\s*=' *.py *.toml ... | grep -v .git | head -15``

    The traversal itself is bounded by :data:`_MAX_SCAN_FILES` per glob — ``rglob`` walks and stats
    every matching file before any line is read, so an unbounded tree (a large ``node_modules``/
    ``.venv``/data directory) costs the same whether or not a match is ever found. Hitting the cap
    prints :data:`SCAN_TRUNCATED_SIGNAL` on stderr rather than silently returning a partial result.

    Returns:
        List of ``path:lineno:content`` match strings, capped at 15.

    Examples:
        >>> isinstance(_grep_version_files(), list)
        True
    """
    results: list[str] = []
    for glob in ("*.py", "*.toml", "*.cfg", "*.json"):
        paths = sorted(itertools.islice(Path(".").rglob(glob), _MAX_SCAN_FILES))
        if len(paths) == _MAX_SCAN_FILES:
            print(SCAN_TRUNCATED_SIGNAL, file=sys.stderr)
        for path in paths:
            if ".git" in path.parts:
                continue
            try:
                if path.stat().st_size > _MAX_SCAN_FILE_SIZE:
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(content.splitlines(), 1):
                if _VERSION_RE.search(line):
                    results.append(f"{path}:{lineno}:{line}")
                    if len(results) >= _MAX_VERSION_LINES:
                        return results
    return results


def _grep_code_signals() -> list[str]:
    """Scan Python files (outside tests) for release-blocking code signals.

    Replicates: ``grep -rn "TODO.*release|FIXME|HACK|XXX" *.py --exclude-dir=tests | head -10``

    Same traversal cap as :func:`_grep_version_files` — see its docstring for why the bound is on
    the iterator, not on the number of matches read.

    Returns:
        List of ``path:lineno:content`` match strings, capped at 10.

    Examples:
        >>> isinstance(_grep_code_signals(), list)
        True
    """
    results: list[str] = []
    paths = sorted(itertools.islice(Path(".").rglob("*.py"), _MAX_SCAN_FILES))
    if len(paths) == _MAX_SCAN_FILES:
        print(SCAN_TRUNCATED_SIGNAL, file=sys.stderr)
    for path in paths:
        if ".git" in path.parts or "tests" in path.parts:
            continue
        try:
            if path.stat().st_size > _MAX_SCAN_FILE_SIZE:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            if _SIGNALS_RE.search(line):
                results.append(f"{path}:{lineno}:{line}")
                if len(results) >= _MAX_SIGNAL_LINES:
                    return results
    return results


def _detect_trunk(git: str) -> str:
    """Detect the default branch name from ``git remote show origin``.

    Args:
        git: Absolute path to the git binary.

    Returns:
        Default branch name; falls back to ``"main"``.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    out = _run([git, "remote", "show", "origin"], timeout=10)
    for line in out.splitlines():
        if "HEAD branch" in line:
            parts = line.split()
            if parts:
                return parts[-1]
    return "main"


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``run_audit_checks.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 1 on bad args; 2 on gh not authenticated or invalid tag; 0 otherwise.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    # Honour only ``-h/--help`` via argparse; every other flag flows through the manual
    # loop below (unknown arg → exit 1, gh/tag guards → exit 2). A broad parse_args
    # would replace those custom exit codes with argparse's exit-2 — keep the loop.
    if args in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="run_audit_checks.py",
            description="Pre-release readiness data gathering for /oss:release audit.",
        ).parse_args(["-h"])

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    repo = ""
    tag = ""
    range_arg = ""

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--repo":
            i += 1
            repo = args[i] if i < len(args) else ""  # noqa: F841
        elif a == "--tag":
            i += 1
            tag = args[i] if i < len(args) else ""
        elif a == "--range":
            i += 1
            range_arg = args[i] if i < len(args) else ""
        else:
            print(f"run_audit_checks: unknown arg '{a}'", file=sys.stderr)
            return 1
        i += 1

    git = _resolve("git")

    if not range_arg:
        last_tag = os.environ.get("LAST_TAG", "")
        if not last_tag:
            desc = _run([git, "describe", "--tags", "--abbrev=0", *_EXCLUDE_TAG_FLAGS])
            if desc:
                last_tag = desc
            else:
                rev = _run([git, "rev-list", "--max-parents=0", "HEAD"])
                last_tag = rev.splitlines()[0] if rev else ""
        # Allowlist: only accept SemVer-style tags or 7-40 hex short SHAs.
        # Anything else (option-style ``--foo``, ref expressions ``HEAD~``,
        # arbitrary text) is rejected outright (A03:2021).
        if not last_tag or not _TAG_OR_SHA_RE.match(last_tag):
            print(
                f"run_audit_checks: invalid tag (must be SemVer or 7-40 hex SHA): {last_tag!r}",
                file=sys.stderr,
            )
            return 2
        range_arg = f"{last_tag}..HEAD"
    else:
        # User-supplied ``--range`` bypasses the auto-derived allowlist above —
        # apply the same allowlist on both endpoints of the range so a crafted
        # value cannot smuggle git option flags (`--upload-pack=…`) or ref
        # expressions (`HEAD~`) past the SemVer/SHA gate (A03:2021).
        # Accept `<endpoint>..<endpoint>` (two-dot) or `<endpoint>...<endpoint>`
        # (three-dot symmetric diff) only; each endpoint must match
        # _TAG_OR_SHA_RE or be the literal `HEAD` (the only ref expression
        # accepted — `HEAD~`, branch names, etc. remain rejected).
        if "..." in range_arg:
            endpoints = range_arg.split("...", maxsplit=1)
        elif ".." in range_arg:
            endpoints = range_arg.split("..", maxsplit=1)
        else:
            endpoints = []

        def _endpoint_ok(ep: str) -> bool:
            return ep == "HEAD" or bool(_TAG_OR_SHA_RE.match(ep))

        if len(endpoints) != 2 or not all(_endpoint_ok(ep) for ep in endpoints):
            print(
                f"run_audit_checks: invalid --range format (expect <tag-or-sha-or-HEAD>..<tag-or-sha-or-HEAD>): {range_arg!r}",
                file=sys.stderr,
            )
            return 2

    # --- Pre-flight: gh authentication ------------------------------------------
    print("--- check: gh-auth ---")
    gh = which("gh")
    if gh is None:
        print("gh not authenticated — run 'gh auth login' first")
        return 2
    auth_proc = subprocess.run(  # noqa: S603
        [gh, "auth", "status"], capture_output=True, text=True, check=False, timeout=10
    )
    combined = auth_proc.stdout + auth_proc.stderr
    if combined:
        print(combined, end="")
    if auth_proc.returncode != 0:
        print("gh not authenticated — run 'gh auth login' first")
        return 2

    # --- Check 1: Repository state -----------------------------------------------
    print("--- check: repo-state ---")
    print("## uncommitted changes:")
    status_out = _run([git, "status", "--short"])
    if status_out:
        print(status_out)
    print(f"## unreleased commits in range {range_arg}:")
    log_out = _run([git, "log", "--oneline", "--no-merges", range_arg, "--"])
    if log_out:
        print(log_out)

    # --- Check 2: CI health ------------------------------------------------------
    print("--- check: ci-health ---")
    branch_out = _run([git, "rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_out or "HEAD"
    ci_out = _run([gh, "run", "list", "--branch", branch, "--limit", "5", "--json", "status,conclusion,name"])
    print(ci_out or "[]")

    # --- Check 3: Open issues and PRs --------------------------------------------
    print("--- check: open-issues-prs ---")
    print("## open issues with high-severity labels:")
    issues_out = _run([gh, "issue", "list", "--state", "open", "--limit", "100", "--json", "number,title,labels"])
    print(issues_out or "[]")
    trunk = _detect_trunk(git)
    print(f"## open PRs targeting {trunk}:")
    prs_out = _run(
        [
            gh,
            "pr",
            "list",
            "--state",
            "open",
            "--base",
            trunk,
            "--limit",
            "20",
            "--json",
            "number,title,draft,reviewDecision",
        ]
    )
    print(prs_out or "[]")

    # --- Check 4: Documentation alignment ----------------------------------------
    print("--- check: docs-alignment ---")
    print(f"## files changed since {range_arg}:")
    diff_files = _run([git, "diff", "--name-only", range_arg, "--"])
    if diff_files:
        print(diff_files)
    print("## docs/README touched:")
    docs_changed = [f for f in diff_files.splitlines() if _DOCS_RE.search(f)]
    if docs_changed:
        print("\n".join(docs_changed))
    else:
        print("no docs changed")

    # --- Check 5: Version consistency --------------------------------------------
    print("--- check: version-consistency ---")
    for match_line in _grep_version_files():
        print(match_line)
    if tag:
        print(f"## target version: {tag}")

    # --- Check 6: Critical code signals ------------------------------------------
    print("--- check: code-signals ---")
    print("## release-blocking TODOs / FIXME / HACK / XXX (outside tests):")
    for match_line in _grep_code_signals():
        print(match_line)
    print("## dependency CVE scan:")
    pip_audit = which("pip-audit")
    if pip_audit:
        parse_script = Path(__file__).parent / "parse_audit_json.py"
        audit_proc = subprocess.run(  # noqa: S603
            [pip_audit, "--format=json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if audit_proc.returncode == 0 and parse_script.is_file():
            parse_proc = subprocess.run(  # noqa: S603
                [sys.executable, str(parse_script)],
                input=audit_proc.stdout,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if parse_proc.returncode == 0:
                print(parse_proc.stdout, end="")
            else:
                print("pip-audit ran but JSON parsing failed")
        else:
            print("pip-audit ran but JSON parsing failed")
    else:
        print(PIP_AUDIT_MISSING_SIGNAL)
        print("pip-audit not installed — CVE scan skipped; install with: pip install pip-audit")

    print("--- check: end ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
