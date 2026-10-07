#!/usr/bin/env python3
"""Resolve CODEMAP_ENABLED from auto/strict/off to true/false.

Python (not shell) because plugins must run on Windows, where a `#!` shebang is
not honoured — see plugins/CLAUDE.md §Installability.

Index location mirrors codemap-py's own resolver (``codemap_py.index_paths.resolve_index``,
consumed by ``query.find_index``) exactly:

* the project root is the **git toplevel**, or the CWD when outside a repository;
* the index directory defaults to ``<root>/.cache/codemap`` — anchored to the root, never
  to the process CWD, so a skill invoked from a subdirectory still finds the index instead
  of reporting a false ``no_index``;
* ``CODEMAP_INDEX_DIR`` overrides that directory as a flat ``<override>/<project>.json``;
* the project name is the **raw** directory basename with no sanitization. The scanner
  writes that name verbatim, so stripping characters here made every repository whose
  directory contains a space, ``+`` or a non-ASCII character resolve to a filename the
  scanner never writes — a permanent, silent ``no_index``.

Also writes currency status to ``${TMPDIR}/<prefix>-${CSID}`` ("current", "stale", "off"
or "no_index"), where ``<prefix>`` is supplied by the calling plugin via
``--currency-prefix``.

This file is kept **byte-identical** across consuming plugins by
``plugins/cc_foundry/bin/propagate_shared.py`` (MANIFEST). Every per-plugin difference
must therefore arrive as an argument from that plugin's own wrapper — never as an edit
here.

Usage:
    codemap_resolve.py [auto|strict|off|true|false] [--currency-prefix PREFIX]

Exits 0: prints "true" or "false" to stdout
Exits 1: strict mode and binary/index missing — error to stderr
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL_LABEL = "codemap-py"  # binary name, warn prefix, and plugin name — all one label
#: Fallback basename prefix of the index-currency sentinel when the caller passes no --currency-prefix.
DEFAULT_CURRENCY_PREFIX = "codemap-currency"
#: Seconds a git subprocess may run before the resolver gives up on it.
_GIT_TIMEOUT_S = 5
_CURRENCY_TIMEOUT_S = 15  # currency check may walk the tree (tier 2); bounded, never unbounded


def _currency_file(prefix: str) -> Path:
    """Return the session-scoped currency sentinel path for *prefix*."""
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    tmp = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return Path(tmp) / f"{prefix}-{csid}"


def _write_currency(prefix: str, value: str) -> None:
    """Record *value* in the currency sentinel, ignoring write failures."""
    try:
        _currency_file(prefix).write_text(value + "\n")
    except OSError:
        pass  # currency status is advisory; never fail the gate on it


def _canonical_root() -> Path:
    """Return the project root — git toplevel, else CWD — symlink-collapsed.

    Mirrors ``codemap_py.index_paths.canonical_root`` so consumer and provider agree on
    both the index directory and the project name.

    ``encoding`` is pinned to UTF-8 — the spelling the provider already uses — because bare
    ``text=True`` decodes with the process's preferred encoding, which is a legacy code page
    on Windows. Git emits the toplevel path as UTF-8 bytes, so any repository whose directory
    name is non-ASCII decoded to mojibake there and resolved to an index filename the scanner
    never writes: a permanent, silent ``no_index`` on exactly the names this resolver stopped
    sanitizing in order to support. ``ValueError`` covers the undecodable-bytes case, which
    falls back to the CWD like any other unusable git answer.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607 - git resolved via PATH on purpose
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return Path.cwd().resolve()


def _index_path(root: Path) -> Path:
    """Return the index file for *root*, honouring the flat ``CODEMAP_INDEX_DIR`` override."""
    override = os.environ.get("CODEMAP_INDEX_DIR")
    index_dir = Path(override).expanduser().resolve() if override else root / ".cache" / "codemap"
    return index_dir / f"{root.name}.json"


def _currency(cic: str, index: Path) -> tuple[str, str]:
    """Return ``(status, reason)`` from a single, timed ``check-index-currency`` run.

    ``check-index-currency`` signals the verdict through its **exit code** (0 current, 1 stale, 2 no_index) while always
    printing the full JSON result to stdout. Gating on ``returncode == 0`` therefore discarded every stale verdict and
    substituted the "current" fallback, so the stale gate could never fire. The verdict is read from stdout and the exit
    code is deliberately ignored.

    One run yields both fields; querying ``--field status`` and ``--field reason`` separately spawned two interpreters,
    and neither call was time-bounded.
    """
    try:
        out = subprocess.run(  # noqa: S603 - argv list, no shell
            [sys.executable, cic, "--index-path", str(index)],
            capture_output=True,
            text=True,
            encoding="utf-8",  # the verdict's `reason` embeds paths; a code-page decode mangles them
            timeout=_CURRENCY_TIMEOUT_S,
            check=False,
        )
        data = json.loads(out.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return "current", ""  # fail open: an unreadable verdict must not block the gate
    if not isinstance(data, dict):
        return "current", ""
    status = data.get("status")
    reason = data.get("reason")
    return (status if isinstance(status, str) and status else "current"), (reason if isinstance(reason, str) else "")


def _record_currency(prefix: str, index: Path) -> None:
    """Probe index currency and record it, warning on stderr when the index is stale."""
    cic = shutil.which("check-index-currency")
    if not cic:
        _write_currency(prefix, "current")
        return
    currency, reason = _currency(cic, index)
    _write_currency(prefix, currency)
    if currency == "stale":
        print(
            f"⚠ {TOOL_LABEL}: index is stale — {reason}\n  Run /codemap-py:scan-codebase to refresh it.",
            file=sys.stderr,
        )


def _unavailable(prefix: str, mode: str, strict_detail: str, warning: str) -> int:
    """Record ``no_index`` and emit the strict or soft outcome; returns the exit code."""
    _write_currency(prefix, "no_index")
    if mode == "strict":
        print(f"! BREAKING — --codemap strict: {strict_detail}", file=sys.stderr)
        return 1
    if warning:
        print(warning, file=sys.stderr)
    print("false")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the resolver's mode and the caller-supplied currency prefix."""
    parser = argparse.ArgumentParser(description="Resolve CODEMAP_ENABLED to true/false.")
    parser.add_argument("mode", nargs="?", default="auto", help="auto | strict | off | true | false")
    parser.add_argument(
        "--currency-prefix",
        default=DEFAULT_CURRENCY_PREFIX,
        help="basename prefix of the currency sentinel; supplied by the calling plugin",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Resolve the codemap mode, print true/false, and record index currency."""
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    mode, prefix = args.mode, args.currency_prefix

    if mode in ("off", "false"):
        print("false")
        _write_currency(prefix, "off")
        return 0
    if mode == "true":
        print("true")
        return 0

    if not shutil.which(TOOL_LABEL):
        return _unavailable(prefix, mode, f"{TOOL_LABEL} query not found. Install {TOOL_LABEL} plugin.", "")

    root = _canonical_root()
    index = _index_path(root)
    if not index.is_file():
        return _unavailable(
            prefix,
            mode,
            f"index {index} not found. Run /codemap-py:scan-codebase first.",
            f"⚠ {TOOL_LABEL}: no index for project '{root.name}' at {index}\n"
            "  Run /codemap-py:scan-codebase to build it, then re-run this skill.",
        )

    _record_currency(prefix, index)
    print("true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
