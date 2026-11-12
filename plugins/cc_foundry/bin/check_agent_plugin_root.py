#!/usr/bin/env python
"""Flag executable plugin Markdown that defaults the host's plugin-root token.

A plugin agent's Bash does not export ``CLAUDE_PLUGIN_ROOT`` (probed in a spawned plugin agent), but the host
text-substitutes the exact token ``${CLAUDE_PLUGIN_ROOT}`` in the agent's prompt body with the install path. The
shell-default form ``${CLAUDE_PLUGIN_ROOT:-plugins/<plugin>}`` is a different string: it is not substituted, the unset
variable expands to its repo-relative default, and every sidecar or ``_shared`` load resolves to a path that exists only
inside this repository. An installed plugin then silently loses the file it meant to load. Skills also require the
plain host-substituted token; loaded modes, templates and sidecars require the owning plugin's literal renderer.

Findings, one per offending line: ``plugin-root-default: <path>:<line>: <text>``. A line carrying
``plugin-root-check: allow`` is an explicit, reviewed exemption.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_agent_plugin_root.py" --scan-dir plugins
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_agent_plugin_root.py" path/to/agent.md [...]

Exit codes:
    0 — no findings
    1 — one or more findings
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Matches the shell-default spelling that the host does not substitute.
_DEFAULTED_ROOT = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT:-")
#: Marker text that exempts a line from the check.
_ALLOW = "plugin-root-check: allow"
#: Executable document scopes, including nested loaded fragments.
_DOCUMENT_DIRS = {"agents", "skills", "claude-skills", "modes", "templates", "references"}
#: Longest finding text printed, so a minified line cannot flood the report.
_TEXT_LIMIT = 160


@dataclass(frozen=True)
class Finding:
    """One executable document line that defaults the plugin root."""

    path: str
    line: int
    text: str


def defaults_plugin_root(line: str) -> bool:
    """Report whether one line uses the defaulted plugin-root form without an exemption.

    Args:
        line: One line of agent Markdown.

    Returns:
        True when the line is a finding.

    Examples:
        >>> defaults_plugin_root('cat "${CLAUDE_PLUGIN_ROOT:-plugins/p}/x.md"')
        True
        >>> defaults_plugin_root('cat "${CLAUDE_PLUGIN_ROOT}/x.md"')
        False
        >>> defaults_plugin_root('cat "${CLAUDE_PLUGIN_ROOT:-p}/x.md"  plugin-root-check: allow')
        False
    """
    return bool(_DEFAULTED_ROOT.search(line)) and _ALLOW not in line


def scan_file(path: Path) -> list[Finding]:
    """Return a finding for every line that uses the defaulted plugin-root form.

    Args:
        path: Plugin Markdown file.

    Returns:
        One finding per offending line; an unreadable file yields none.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [
        Finding(path.as_posix(), number, text.strip()[:_TEXT_LIMIT])
        for number, text in enumerate(lines, start=1)
        if defaults_plugin_root(text)
    ]


def _is_live_document(path: Path) -> bool:
    """Exclude historical fixtures and changelogs from executable document checks."""
    return path.suffix == ".md" and path.name != "CHANGELOG.md" and not {"tests", "fixtures"}.intersection(path.parts)


def collect(scan_dir: Path | None, files: list[str]) -> list[Path]:
    """Collect explicit Markdown files and executable document scopes under the scan directory.

    Args:
        scan_dir: Directory searched recursively for executable plugin Markdown, or None.
        files: Explicit Markdown file paths.

    Returns:
        Sorted, de-duplicated file paths.
    """
    found = {Path(f) for f in files if _is_live_document(Path(f))}
    if scan_dir is not None:
        found.update(
            p
            for p in scan_dir.rglob("*.md")
            if p.is_file() and _is_live_document(p) and _DOCUMENT_DIRS.intersection(p.parts)
        )
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    """Print every finding and return the exit code.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        0 when clean, 1 when any finding exists.
    """
    parser = argparse.ArgumentParser(description="Flag defaulted plugin-root tokens in executable plugin Markdown.")
    parser.add_argument("--scan-dir", type=Path, help="directory whose skills, agents and loaded fragments are scanned")
    parser.add_argument("files", nargs="*", help="explicit Markdown files")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    findings = [finding for path in collect(args.scan_dir, args.files) for finding in scan_file(path)]
    for finding in findings:
        print(f"plugin-root-default: {finding.path}:{finding.line}: {finding.text}")
    if not findings:
        print("✓ agent-plugin-root: no executable document defaults the host plugin-root token")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
