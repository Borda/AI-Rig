#!/usr/bin/env python
"""search_downstream_consumers.py — find GitHub repos importing changed symbols.

Loops over symbol names (argv or stdin, one per line) and queries the GitHub
code-search API via ``gh``. Prints the union of repo ``full_name`` values
(sorted, deduplicated) so the caller can warn downstream maintainers before
shipping a breaking change.

Usage:
    search_downstream_consumers.py --package <name> [<symbol> ...]
    echo -e "Symbol1\\nSymbol2" | search_downstream_consumers.py --package <name>

Exit codes:
    0 — search ran (empty result acceptable — no downstream consumers found)
    1 — bad args (missing ``--package`` or no symbols provided)
    2 — gh CLI failure on every symbol query
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from shutil import which

#: GitHub code-search query strings must not carry quote/colon/.. or shell-special
#: characters — those let an attacker pollute the search operators or break out
#: of the embedded query.  We accept dotted Python identifiers (e.g.
#: ``foo.bar.Baz``), digits, hyphen, and underscore — nothing else.
_QUERY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
#: Substrings rejected in a search query argument because they could alter operators or escape the query.
_FORBIDDEN_QUERY_SUBSTRINGS: tuple[str, ...] = ("..", '"', ":", "`", "$", "(", ")", "<", ">", "\\")


def _sanitize_query_arg(value: str, label: str) -> str:
    r"""Return the stripped value if it is safe for the gh code-search query.

    Args:
        value: Raw argv token.
        label: Field name used in the error message.

    Returns:
        The whitespace-stripped value.

    Raises:
        ValueError: When the value is empty, contains forbidden substrings, or
            characters outside ``[A-Za-z0-9_.\-]``.

    Examples:
        >>> _sanitize_query_arg("foo.bar", "symbol")
        'foo.bar'
        >>> _sanitize_query_arg("  pkg-name  ", "package")
        'pkg-name'
        >>> _sanitize_query_arg('bad"sym', "symbol")
        Traceback (most recent call last):
            ...
        ValueError: symbol contains disallowed characters or substrings: 'bad"sym'
        >>> _sanitize_query_arg("..", "symbol")
        Traceback (most recent call last):
            ...
        ValueError: symbol contains disallowed characters or substrings: '..'
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{label} must not be empty after stripping whitespace")
    if any(sub in stripped for sub in _FORBIDDEN_QUERY_SUBSTRINGS):
        raise ValueError(f"{label} contains disallowed characters or substrings: {stripped!r}")
    if not _QUERY_TOKEN_RE.match(stripped):
        raise ValueError(f"{label} contains disallowed characters or substrings: {stripped!r}")
    return stripped


def _resolve(cmd: str) -> str:
    """Resolve a CLI tool to its absolute path.

    Args:
        cmd: Bare executable name (e.g. ``"gh"``).

    Returns:
        Absolute path to the executable.

    Raises:
        FileNotFoundError: If ``cmd`` is not present on ``PATH``.

    Examples:
        >>> import shutil
        >>> _resolve(sys.executable) == shutil.which(sys.executable)
        True
    """
    p = which(cmd)
    if p is None:
        raise FileNotFoundError(f"executable not found on PATH: {cmd}")
    return p


def search_consumers(package: str, symbols: list[str]) -> tuple[int, set[str]]:
    """Query GitHub code search for each symbol; return success count and repo set.

    For each symbol, runs a ``gh api search/code`` query for imports of that symbol
    and extracts repository names from the response.

    Args:
        package: Python package name used in the import query.
        symbols: Symbol names to search for.

    Returns:
        Tuple of (number of successful queries, set of repo ``full_name`` strings).

    Examples:
        No doctest — requires subprocess; covered by pytest with monkeypatch.
    """
    gh = _resolve("gh")
    repos: set[str] = set()
    successes = 0
    for sym in symbols:
        try:
            proc = subprocess.run(  # noqa: S603
                [
                    gh,
                    "api",
                    "search/code",
                    "--field",
                    f"q=from {package} import {sym} language:python",
                    "--paginate",
                    "--jq",
                    ".items[].repository.full_name",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            print(f"⚠ search timed out after 60s for symbol {sym!r} (non-fatal)", file=sys.stderr)
            continue
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                stripped = line.strip()
                if stripped:
                    repos.add(stripped)
            successes += 1
        else:
            print(f"⚠ search failed for symbol {sym!r} (non-fatal)", file=sys.stderr)
    return successes, repos


def _parse_args(args: list[str]) -> tuple[str, list[str]]:
    """Separate ``--package`` from bare symbol positionals in the manual argv loop.

    Args:
        args: Raw argv tokens (``-h``/``--help`` already handled by the caller).

    Returns:
        ``(package, symbols)`` — ``package`` is empty when ``--package`` was never given.
    """
    package = ""
    symbols: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--package" and i + 1 < len(args):
            package = args[i + 1]
            i += 2
        else:
            symbols.append(args[i])
            i += 1
    return package, symbols


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``search_downstream_consumers.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code.

    Examples:
        No doctest — requires subprocess; covered by pytest with monkeypatch.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    # Honour only ``-h/--help`` via argparse; the manual loop below treats every other
    # token as a symbol (or ``--package`` value), so a broad parse_args would misread
    # symbol positionals — keep the legacy passthrough intact.
    if args in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="search_downstream_consumers.py",
            description="Find GitHub repos importing changed symbols via gh code search.",
        ).parse_args(["-h"])

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    package, symbols = _parse_args(args)
    if not package:
        print("search_downstream_consumers: --package required", file=sys.stderr)
        return 1
    if not symbols and not sys.stdin.isatty():
        for line in sys.stdin:
            stripped = line.rstrip("\n")
            if stripped:
                symbols.append(stripped)
    if not symbols:
        print("search_downstream_consumers: no symbols provided (argv or stdin)", file=sys.stderr)
        return 1

    # Sanitize before they reach the gh search query.  Reject the whole
    # invocation rather than dropping individual entries — search operator
    # pollution from a single bad token would skew the union result silently.
    try:
        package = _sanitize_query_arg(package, "package")
        symbols = [_sanitize_query_arg(s, "symbol") for s in symbols]
    except ValueError as exc:
        print(f"search_downstream_consumers: {exc}", file=sys.stderr)
        return 1

    successes, repos = search_consumers(package, symbols)
    if successes == 0:
        print("search_downstream_consumers: all symbol queries failed", file=sys.stderr)
        return 2
    for repo in sorted(repos):
        print(repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
