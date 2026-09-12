#!/usr/bin/env python3
"""detect_codemap.py — detect codemap plugin availability, index presence, and currency.

consumers: resolve/SKILL.md, review/SKILL.md, analyse/modes/codemap-signals.md

Determines whether the codemap plugin is installed and an index exists for the
current project.  Writes result to ${TMPDIR:-/tmp}/<prefix>-codemap-enabled-<CSID>.
When check-index-currency is on PATH, also writes currency status to
${TMPDIR:-/tmp}/<prefix>-codemap-currency-<CSID> ("current", "stale", or "no_index").

Usage:
    python detect_codemap.py --prefix resolve [--arguments "$ARGUMENTS"] [--force-off]
                             [--strict] [--proj <name>]

Flags:
    --prefix <name>   Prefix for temp-file name (e.g. resolve, review). Required.
    --arguments <s>   Raw skill argument text. `--no-codemap` in it implies --force-off;
                      `--codemap` alone implies --strict. Matched as whole tokens, so a
                      longer flag or a quoted value never triggers either. Lets the caller
                      hand over one argv slot instead of interpolating $ARGUMENTS into
                      shell tests, where an embedded quote breaks the block at parse time.
    --force-off       CODEMAP_FORCE_OFF=true — always write false.
    --strict          CODEMAP_STRICT=true — exit 1 when codemap absent/index missing.
    --proj <name>     Project name override (default: basename of git toplevel).
    --idx-dir <path>  Codemap index directory override (default: <git toplevel>/.cache/codemap).

Temp files written:
    ${TMPDIR:-/tmp}/<prefix>-codemap-enabled-<CSID>   → "true" or "false"
    ${TMPDIR:-/tmp}/<prefix>-codemap-currency-<CSID>  → "current", "stale", or "no_index"

Exit codes:
    0   success (CODEMAP_ENABLED written)
    1   ``--strict`` mode: codemap not installed or index missing (error printed)
    2   missing required ``--prefix`` argument
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _check_currency(index_path: Path) -> tuple[str, str]:
    """Return ``(status, reason)`` for an existing index, failing open to ``current``.

    ``check-index-currency`` is optional; when it is absent, times out, or emits
    unparsable output the gate deliberately fails **open** — a staleness probe must
    never block a skill that already has a usable index. The coercion is announced on
    stderr rather than applied silently, so "current" that was assumed is
    distinguishable from "current" that was measured.

    Args:
        index_path: Path to the codemap index JSON that was already found.

    Returns:
        ``(status, reason)`` — status is one of the probe's own values, or
        ``"current"`` when the probe could not be consulted.
    """
    currency_bin = shutil.which("check-index-currency")
    if not currency_bin:
        return "current", ""
    try:
        result = subprocess.run(
            [sys.executable, currency_bin, "--index-path", str(index_path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        data = json.loads(result.stdout.strip())
        return str(data.get("status", "current")), str(data.get("reason", ""))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(
            f"⚠ codemap-py: currency probe failed ({type(exc).__name__}) — assuming 'current'; "
            "index staleness was NOT verified.",
            file=sys.stderr,
        )
        return "current", ""


def _project_root() -> Path:
    """Return the project root the codemap index is filed under.

    Mirrors the provider's own resolver (``codemap_py.index_paths.canonical_root``):
    the git top-level when the process runs inside a repository, otherwise the CWD.
    Anchoring here — rather than trusting the CWD — is what lets a skill invoked from
    a repo subdirectory still find the index the scanner wrote at the repo root.

    Returns:
        Absolute project root path (git top-level, else the current directory).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return Path.cwd()
    top = result.stdout.strip() if result.returncode == 0 else ""
    return Path(top) if top else Path.cwd()


def _resolve_proj(proj_override: str | None, root: Path) -> str:
    """Return the project name the index file is named after.

    The provider names the index after the **raw** basename of the project root
    (``codemap_py.index_paths.resolve_index`` → ``base_root.name``) with no
    sanitization. A consumer that strips characters would seek a filename the
    scanner never wrote — a permanent false ``no_index`` for any repository whose
    directory name contains a space, ``+``, or a non-ASCII character.

    Args:
        proj_override: Explicit ``--proj`` value; wins when non-empty.
        root: Project root from :func:`_project_root`.

    Returns:
        Project name used as ``<name>.json`` under the index directory.

    Examples:
        >>> _resolve_proj(None, Path("work/my-project"))
        'my-project'
        >>> _resolve_proj("checkout", Path("work/my-project"))
        'checkout'
    """
    return proj_override or root.name


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code.
    """
    args = list(sys.argv[1:] if argv is None else argv)

    # Honour only ``-h/--help`` via argparse; every other flag flows through the manual
    # loop below, which ignores unknowns and keeps the legacy exit-2-on-missing-prefix
    # contract (argparse's native errors would change both behaviors).
    if args in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="detect_codemap.py",
            description="Detect codemap plugin availability, index presence, and currency.",
        ).parse_args(["-h"])

    prefix: str | None = None
    force_off = False
    strict = False
    arguments: str | None = None
    proj_override: str | None = None
    # None = "not overridden" → derived from the project root below. An explicit
    # ``--idx-dir`` wins over CODEMAP_INDEX_DIR, which wins over the default layout.
    idx_dir: str | None = os.environ.get("CODEMAP_INDEX_DIR") or None

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--prefix" and i + 1 < len(args):
            prefix = args[i + 1]
            i += 2
        elif a == "--force-off":
            force_off = True
            i += 1
        elif a == "--strict":
            strict = True
            i += 1
        elif a == "--arguments" and i + 1 < len(args):
            arguments = args[i + 1]
            i += 2
        elif a.startswith("--arguments="):
            arguments = a[len("--arguments=") :]
            i += 1
        elif a == "--proj" and i + 1 < len(args):
            proj_override = args[i + 1]
            i += 2
        elif a == "--idx-dir" and i + 1 < len(args):
            idx_dir = args[i + 1]
            i += 2
        else:
            i += 1

    if not prefix:
        print(
            'Usage: detect_codemap.py --prefix <name> [--force-off] [--strict] [--arguments "$ARGUMENTS"]',
            file=sys.stderr,
        )
        return 2

    # Derive the two modes from the skill's raw argument text, so the caller passes one argv
    # slot instead of interpolating $ARGUMENTS into shell tests. shlex, not str.split: a
    # plain split leaves `--keep "use --codemap later"` looking like a --codemap request,
    # because the quotes stay attached to their neighbours instead of grouping the value.
    # An explicit --force-off/--strict still wins; this only adds.
    if arguments is not None:
        try:
            tokens = shlex.split(arguments)
        except ValueError:
            # Unbalanced quote in the argument text. Degrade to the coarser split rather
            # than abort: a codemap-mode guess is not worth failing the whole skill over.
            tokens = arguments.split()
        if "--no-codemap" in tokens:
            force_off = True
        elif "--codemap" in tokens:
            strict = True

    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    tmpdir = os.environ.get("TMPDIR") or tempfile.gettempdir()
    out_file = Path(tmpdir) / f"{prefix}-codemap-enabled-{csid}"
    currency_file = Path(tmpdir) / f"{prefix}-codemap-currency-{csid}"
    # Distinct from `enabled`: the user asking for --no-codemap is not the same as codemap
    # being unavailable, and the Gate A/B machinery branches on the difference. Callers that
    # used to derive this from an inline shell test read it back from here instead.
    (Path(tmpdir) / f"{prefix}-codemap-forced-off-{csid}").write_text(
        f"{str(force_off).lower()}\n", encoding="utf-8", newline="\n"
    )

    if force_off:
        out_file.write_text("false\n")
        currency_file.write_text("off\n")
        return 0

    root = _project_root()
    proj = _resolve_proj(proj_override, root)
    scan_query_available = shutil.which("codemap-py") is not None
    index_dir = Path(idx_dir) if idx_dir else root / ".cache" / "codemap"
    index_path = index_dir / f"{proj}.json"
    # is_file, not exists: a *directory* named "<proj>.json" would otherwise pass the
    # gate and every downstream query would then fail against an unreadable index.
    index_found = index_path.is_file()

    if not scan_query_available or not index_found:
        if strict:
            if not scan_query_available:
                print(
                    "! --codemap passed but codemap plugin not installed.\n"
                    "  Install: claude plugin install codemap-py@borda-ai-rig",
                    file=sys.stderr,
                )
            else:
                print(
                    f"! --codemap passed but no index found for project '{proj}'.\n"
                    "  Build index: /codemap-py:scan-codebase (requires codemap-py plugin)",
                    file=sys.stderr,
                )
            return 1
        if scan_query_available and not index_found:
            print(
                f"⚠ codemap-py: no index for project '{proj}' at {index_path}\n"
                "  Run /codemap-py:scan-codebase to build it, then re-run this skill.",
            )
        currency_file.write_text("no_index\n")
        out_file.write_text("false\n")
        return 0

    currency, currency_reason = _check_currency(index_path)
    currency_file.write_text(f"{currency}\n")

    if currency == "stale":
        print(
            f"⚠ codemap-py: index is stale — {currency_reason}\n  Run /codemap-py:scan-codebase to refresh it.",
        )

    out_file.write_text("true\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
