#!/usr/bin/env python3
"""Codemap-py source-tree bootstrap.

Single Python entrypoint shared by POSIX launchers, Windows launchers, an
editable developer install, and runtime skills. It resolves the real plugin
root, validates the running interpreter, prepends ``<plugin-root>/src`` to its
own process import path, and hands control to :mod:`codemap_py.cli` without
remapping arguments.

The dispatcher now lives in the importable
``codemap_py`` package; the entry now imports ``codemap_py.cli`` directly
instead of the transitional ``scripts/``-relative ``codemap_py_cli`` shim. It
performs no install, download, cache mutation, or dependency setup.

Examples:
    Run from a POSIX launcher::

        python3 scripts/codemap_py_entry.py doctor --json
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Interpreter bound is duplicated (not imported) so the gate runs *before* any
# codemap import, honouring  "validated before importing codemap_py".
#: Required CPython major version.
_MAJOR = 3
#: Lowest supported CPython 3 minor version (3.11).
_MIN_MINOR = 11
#: First CPython 3 minor version that is not supported (3.15).
_MAX_MINOR_EXCLUSIVE = 15
#: Message template printed to stderr when the running interpreter is outside the supported range.
_INTERPRETER_DIAGNOSTIC = (
    "codemap-py: unsupported interpreter {impl} {major}.{minor}; "
    "requires CPython >=3.11,<3.15 (set CODEMAP_PYTHON to an eligible interpreter)"
)
#: Exit code returned after printing the unsupported-interpreter diagnostic.
_INTERPRETER_EXIT = 127


def _interpreter_supported() -> bool:
    """Return whether the running interpreter satisfies the CPython bound."""
    info = sys.version_info
    return (
        sys.implementation.name == "cpython"
        and info.major == _MAJOR
        and _MIN_MINOR <= info.minor < _MAX_MINOR_EXCLUSIVE
    )


def main() -> int:
    """Validate the interpreter, wire the import roots, and delegate to the CLI.

    Returns:
        The dispatcher exit code, or ``127`` when the running interpreter is not
        an eligible CPython. On rejection stdout stays empty and a single
        actionable diagnostic is written to stderr.

    Examples:
        >>> isinstance(main, object)
        True
    """
    if not _interpreter_supported():
        info = sys.version_info
        sys.stderr.write(
            _INTERPRETER_DIAGNOSTIC.format(impl=sys.implementation.name, major=info.major, minor=info.minor) + "\n"
        )
        return _INTERPRETER_EXIT

    root = Path(__file__).resolve().parents[1]
    if len(sys.argv) >= 2 and sys.argv[1] == "--run-hook":
        # Only shipped hooks enter this path; no arbitrary script execution API.
        hooks = {
            "record-exhausted.py",
            "log-tool-use.py",
            "log-skill-start.py",
            "guard-redundant-scan.py",
            "seed-session.py",
            "inject-preamble.py",
        }
        if len(sys.argv) != 3 or sys.argv[2] not in hooks:
            sys.stderr.write("codemap-py: --run-hook requires a shipped hook basename\n")
            return 2
        os.execv(sys.executable, [sys.executable, str(root / "hooks" / sys.argv[2])])  # noqa: S606 - fixed interpreter, hook name from plugin-owned argv
    src = root / "src"
    if src.is_dir():
        entry = str(src)
        if entry not in sys.path:
            sys.path.insert(0, entry)

    from codemap_py.cli import main as cli_main  # imported after the interpreter gate and path wiring

    return cli_main(sys.argv[1:], plugin_root=root)


if __name__ == "__main__":
    raise SystemExit(main())
