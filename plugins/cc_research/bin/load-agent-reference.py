#!/usr/bin/env python3
"""load-agent-reference.py — resolve an agent's references/ sidecar dir and emit one fragment.

Source tree first, plugin cache second (the installed layout has no plugins/ dir). Missing
sidecar dir is fatal — the agent cannot run degraded without knowing its own reference set.
A missing individual fragment is not: the caller supplies the degraded-mode line to print
instead, because that text names per-fragment fallbacks and is genuinely different per doc.

Usage: python load-agent-reference.py <agent-dir-name> <fragment.md> <degraded-msg> || exit 1
Exit codes: 0 = fragment or degraded message emitted · 1 = sidecar dir unresolved · 2 = bad args
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from pathlib import Path

_AGENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FRAGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _is_safe_fragment(value: str) -> bool:
    """Report whether ``value`` is a single, non-traversing filename component.

    Uses an allowlist (not a denylist) so a Windows drive-relative form like
    ``"C:foo.md"`` is rejected too — ``PurePath`` joins reset to a drive-qualified
    right-hand operand, letting it escape the sidecar directory regardless of the left
    operand. ``agent`` is validated separately against :data:`_AGENT_RE` (it also feeds
    an ``fnmatch`` glob in :func:`_cached_sidecar`); this check covers ``fragment``,
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


def _cached_sidecar(agent: str) -> Path | None:
    """First installed-cache dir matching */research/*/references/<agent>, as `find | head -1`."""
    cache = Path.home() / ".claude" / "plugins" / "cache"
    pattern = f"*/research/*/references/{agent}"
    for parent, dirnames, _ in os.walk(cache):
        for dirname in dirnames:
            candidate = os.path.join(parent, dirname)
            if fnmatch.fnmatch(candidate, pattern):
                return Path(candidate)
    return None


def _sidecar_dir(agent: str) -> Path | None:
    """Source-tree sidecar if present, else the installed-cache copy, else None."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or "plugins/cc_research"
    local = Path(root) / "references" / agent
    if local.is_dir():
        return local
    cached = _cached_sidecar(agent)
    return cached if cached is not None and cached.is_dir() else None


def main(argv: list[str]) -> int:
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
