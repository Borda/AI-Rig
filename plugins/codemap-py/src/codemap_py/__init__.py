"""codemap_py — dual-runtime Python structural index package.

Houses the index data contract (:mod:`codemap_py.schema`), the canonical
project-index resolver (:mod:`codemap_py.index_paths`), the cross-process
read/write gate (:mod:`codemap_py.rwgate`), runtime-scoped logging
(:mod:`codemap_py.runtime_log`, :mod:`codemap_py.telemetry`), file discovery
and AST scanning (:mod:`codemap_py.scanner`), cross-module graph construction
and scan orchestration (:mod:`codemap_py.graph`), the query engine
(:mod:`codemap_py.query`), and the CLI dispatcher (:mod:`codemap_py.cli`).
``bin/scan-index``, ``bin/scan-query``, and the other ``bin/_*`` modules are
thin launchers or compatibility shims over this package.

Examples:
    >>> isinstance(__version__, str)
    True
"""

from __future__ import annotations

import json
from pathlib import Path

#: Plugin root directory (two levels above the package), used to locate .claude-plugin/plugin.json for the version.
_PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _read_version() -> str:
    """Return the codemap-py plugin version from its manifest, or ``"?"`` if unreadable.

    Mirrors the read-once pattern used by :func:`codemap_py.telemetry.plugin_version`
    so both the package identity and telemetry records derive from the one
    manifest field.

    Examples:
        >>> isinstance(_read_version(), str)
        True
    """
    try:
        manifest = _PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
        return str(json.loads(manifest.read_text()).get("version", "?"))
    except Exception:
        return "?"


__version__ = _read_version()

__all__ = ["__version__"]
