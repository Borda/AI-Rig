#!/usr/bin/env python
"""locate_scan_query.py — resolve the scan-query executable via three-tier fallback.

Tiers (first hit wins):
    1. ``scan-query`` on PATH (``shutil.which``), absolute hits only
    2. ``${CLAUDE_PLUGIN_ROOT}/bin/scan-query``
    3. ``~/.claude/plugins/cache/borda-ai-rig/codemap-py/*/bin/scan-query`` (newest semver)

Usage:
    locate_scan_query.py

Output:
    Resolved absolute path on stdout (LF-terminated); nothing else.

Exit codes:
    0 — found and executable
    1 — not found (error message on stderr)
    2 — bad/missing required argument (argparse default)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

#: Marketplace and plugin directory names under ``~/.claude/plugins/cache`` that tier 3 trusts; globbing every
#: marketplace would accept an executable from any plugin that merely ships a matching directory name.
_CACHE_MARKETPLACE = "borda-ai-rig"
_CACHE_PLUGIN = "codemap-py"


def _find_executable(path: Path) -> Path | None:
    """Return *path* if it exists and is executable; ``None`` otherwise.

    On Windows also probes the ``.exe`` variant when the bare name is absent.

    Args:
        path: Candidate path (may or may not exist).

    Returns:
        Executable path if found, ``None`` if not.
    """
    if path.is_file() and os.access(path, os.X_OK):
        return path
    if sys.platform == "win32":
        exe = path.with_suffix(".exe")
        if exe.is_file() and os.access(exe, os.X_OK):
            return exe
    return None


def _version_key(path: Path) -> list[int]:
    """Extract semver digits from the version directory in *path* for sorting.

    Expects a path segment like ``…/codemap-py/<version>/bin/…`` and returns
    the numeric components of ``<version>`` so ``max()`` picks the newest.

    Args:
        path: Path containing a version segment one level above ``bin/``.

    Returns:
        List of ints representing the version (e.g. ``[0, 3, 2]``).
    """
    parts = path.parts
    try:
        bin_idx = next(i for i, p in enumerate(parts) if p == "bin")
        return [int(t) for t in re.findall(r"\d+", parts[bin_idx - 1])]
    except StopIteration:
        return [0]


def locate_scan_query() -> Path:
    """Resolve the scan-query executable via a three-tier fallback cascade.

    Returns:
        Absolute path to the scan-query executable.

    Raises:
        FileNotFoundError: When no tier locates an executable.
    """
    # Tier 1 — PATH. A relative hit comes from a relative PATH entry (".", "") and so depends on the
    # working directory, which may be an untrusted checkout; skip it and fall through to the pinned tiers.
    found = shutil.which("scan-query")
    if found and os.path.isabs(found) and os.access(found, os.X_OK):
        return Path(found)

    # Tier 2 — canonicalized CLAUDE_PLUGIN_ROOT, which guards against symlink and relative-path tricks.
    # Only trust an absolute env value; reject empty or relative paths (skip the tier entirely).
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if plugin_root and Path(plugin_root).is_absolute():
        plugin_root_path = Path(plugin_root).resolve()
        result = _find_executable(plugin_root_path / "bin" / "scan-query")
        # Containment guard — the resolved candidate must stay inside plugin_root; a planted
        # symlink at bin/scan-query that escapes the plugin root is rejected, falling through to Tier 3.
        if result:
            try:
                if result.resolve().is_relative_to(plugin_root_path):
                    return result
            except OSError:
                pass  # unresolvable candidate (broken symlink, permission error) — skip to Tier 3

    # Tier 3 — pinned marketplace/plugin cache glob, newest semver
    # Apply the same containment guard as Tier 2: reject a cache symlink that escapes
    # cache_base rather than trusting the glob match alone. cache_base is resolved so a
    # symlinked ~/.claude does not make every candidate look like an escape.
    cache_base = (Path.home() / ".claude" / "plugins" / "cache").resolve()
    plugin_cache = cache_base / _CACHE_MARKETPLACE / _CACHE_PLUGIN
    glob_hits = list(plugin_cache.glob("*/bin/scan-query"))
    if sys.platform == "win32":
        glob_hits += list(plugin_cache.glob("*/bin/scan-query.exe"))
    candidates = []
    for hit in glob_hits:
        found = _find_executable(hit)
        if found is None:
            continue
        try:
            if found.resolve().is_relative_to(cache_base):
                candidates.append(found)
        except OSError:
            pass  # unresolvable candidate (broken symlink, permission error) — skip
    if candidates:
        return max(candidates, key=_version_key)

    raise FileNotFoundError("scan-query binary not found (PATH, CLAUDE_PLUGIN_ROOT, plugin cache all empty)")


def main(argv: list[str] | None = None) -> int:
    """Locate and print the preferred ``scan-query`` executable.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``). The script takes
            no positional arguments; argparse provides ``-h``/``--help`` only.

    Returns:
        0 on success, 1 when scan-query is not found.
    """
    parser = argparse.ArgumentParser(
        prog="locate_scan_query.py",
        description="Resolve the scan-query executable via three-tier fallback and print its path.",
    )
    parser.parse_args(argv)  # no arguments — enables -h/--help, argparse exits 2 on unexpected tokens

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    try:
        sq = locate_scan_query()
    except FileNotFoundError as exc:
        print(f"locate_scan_query: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(str(sq) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
