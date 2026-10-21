#!/usr/bin/env python3
"""load-agent-reference.py — resolve an agent's references/ sidecar dir and emit one fragment.

Source tree first, plugin cache second (the installed layout has no plugins/ dir). With
``CLAUDE_PLUGIN_ROOT`` unset, the cwd-relative ``plugins/cc_research`` tree counts as the source
tree only when the working directory is the research plugin's own source repository (its
``plugins/cc_research/.claude-plugin/plugin.json`` names ``research``); anywhere else it is the
text of the repository the session was opened in, so it is skipped with a stderr notice. Missing
sidecar dir is fatal — the agent cannot run degraded without knowing its own reference set.
A missing individual fragment is not: the caller supplies the degraded-mode line to print
instead, because that text names per-fragment fallbacks and is genuinely different per doc.

Usage: python load-agent-reference.py <agent-dir-name> <fragment.md> <degraded-msg> || exit 1
Exit codes: 0 = fragment or degraded message emitted · 1 = sidecar dir unresolved · 2 = bad args
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from resolve_shared import source_repo_marker_present  # sibling bin/ script; same source-repository gate

#: Allowed shape of an agent name used to build a reference path: letters, digits, underscore and hyphen only.
_AGENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
#: Allowed shape of a reference fragment name: letters, digits, underscore, dot and hyphen only.
_FRAGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _is_safe_fragment(value: str) -> bool:
    """Report whether ``value`` is a single, non-traversing filename component.

    Uses an allowlist (not a denylist) so a Windows drive-relative form like
    ``"C:foo.md"`` is rejected too — ``PurePath`` joins reset to a drive-qualified
    right-hand operand, letting it escape the sidecar directory regardless of the left
    operand. ``agent`` is validated separately against :data:`_AGENT_RE` (it also feeds
    a ``Path.glob`` pattern in :func:`_cached_sidecar`); this check covers ``fragment``,
    which is joined onto the resolved sidecar directory and never globbed.

    Examples:
        >>> _is_safe_fragment("storage-patterns.md")
        True
        >>> _is_safe_fragment("../etc/passwd")
        False
        >>> _is_safe_fragment("/etc/passwd")
        False
        >>> _is_safe_fragment("C:foo.md")
        False
        >>> _is_safe_fragment("..")
        False
        >>> _is_safe_fragment(".")
        False
    """
    if value in (".", ".."):
        return False
    return bool(_FRAGMENT_RE.fullmatch(value))


def _version_key(name: str) -> list[int]:
    """Sort key for a cache version directory name: its digit runs as integers.

    Examples:
        >>> _version_key("0.9.0") < _version_key("0.29.0")
        True
    """
    return [int(run) for run in re.findall(r"\d+", name)]


def _cached_sidecar(agent: str) -> Path | None:
    """Newest non-orphaned installed-cache dir ``<marketplace>/research/<version>/references/<agent>``.

    The glob is fixed-depth, so only the research plugin's own version directories are visited — never a walk of the
    whole plugin cache. A version carrying ``.orphaned_at`` is a superseded install and is skipped.
    """
    cache = Path.home() / ".claude" / "plugins" / "cache"
    candidates = [
        sidecar
        for sidecar in cache.glob(f"*/research/*/references/{agent}")
        if sidecar.is_dir() and not (sidecar.parent.parent / ".orphaned_at").exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda sidecar: _version_key(sidecar.parent.parent.name))


def _sidecar_dir(agent: str) -> Path | None:
    """Plugin-root or trusted source-tree sidecar if present, else the installed-cache copy, else None."""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    root = plugin_root or "plugins/cc_research"
    local = Path(root) / "references" / agent
    if local.is_dir():
        if plugin_root:
            return local
        # A cwd-relative tree is whatever repository the session was opened in; trust it only in the source repo.
        if source_repo_marker_present():
            print(
                "load-agent-reference: CLAUDE_PLUGIN_ROOT unset — reading cwd-relative plugins/cc_research (local dev only)",
                file=sys.stderr,
            )
            return local
        print(
            "load-agent-reference: CLAUDE_PLUGIN_ROOT unset — ignoring cwd-relative plugins/cc_research: the working "
            "directory is not the research plugin source repository (no plugins/cc_research/.claude-plugin/plugin.json "
            "naming research)",
            file=sys.stderr,
        )
    cached = _cached_sidecar(agent)
    return cached if cached is not None and cached.is_dir() else None


def main(argv: list[str]) -> int:
    """Resolve an agent's references/ sidecar directory and emit one fragment."""
    args = argv[1:]
    if len(args) != 3:
        print(
            f"load-agent-reference: expected 3 args (agent-dir-name fragment degraded-msg), got {len(args)}",
            file=sys.stderr,
        )
        return 2

    agent, fragment, degraded = args
    if not agent or not _AGENT_RE.fullmatch(agent):
        print(f"load-agent-reference: <agent-dir-name> must match [A-Za-z0-9_-]+, got {agent!r}", file=sys.stderr)
        return 2
    if not fragment or not _is_safe_fragment(fragment):
        print(
            f"load-agent-reference: <fragment.md> must be a single safe filename component, got {fragment!r}",
            file=sys.stderr,
        )
        return 2

    directory = _sidecar_dir(agent)
    if directory is None:
        print(
            f"! BLOCKED — research:{agent} sidecar not found; ensure research plugin is "
            "installed (claude plugin install research@borda-ai-rig)",
            file=sys.stderr,
        )
        return 1

    # Bytes, as `cat` does: fragments are markdown but nothing here needs to decode them,
    # and a re-encode would risk altering content the caller pastes verbatim.
    try:
        payload = (directory / fragment).read_bytes()
    except OSError:
        print(degraded)
        return 0
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
