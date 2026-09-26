"""Configure pytest imports for shipped Codex Rig executable modules.

## Purpose

make standalone helpers importable during doctest and pytest collection without packaging them as one Python
distribution. This lets the plugin tests exercise installed-style scripts directly while keeping the repository root out
of their import contract.

## Scope

adjusts only test-process ``sys.path`` and skips platform-inapplicable acceptance modules; it never changes plugin
runtime behavior. It also clears canonical script modules around each test so classes loaded from different paths cannot
leak state into later collection or assertions.

## Usage

pytest discovers this file automatically; it is not a supported command-line tool. Run the plugin's pytest suite
normally from the repository or plugin directory and pytest will apply these collection and isolation rules
automatically.

## Used by

the Codex Rig test suite, especially tests that load scripts directly from their installed-style paths. Tests for
Windows-incompatible shim lifecycle modules rely on its platform-specific collection list instead of carrying skip logic
in every test module.

## Outputs

pytest receives deterministic import paths and platform-aware collection behavior; no artifact or package file is
produced. The module-level setup changes only the current pytest process, and the autouse fixture restores previously
loaded modules after each test.

## Failure

an invalid fixture path or an unsupported-platform regression surfaces as a normal collection/test failure instead of
being suppressed. If module isolation cannot be maintained, the affected test fails with its import or assertion error,
preserving evidence for diagnosis.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_WINDOWS_UNSUPPORTED_PATHS = (
    "scripts/_agent_shim_posix.py",
    "scripts/_agent_shim_transaction.py",
    "tests/agent_shim/test_agent_shim_posix.py",
    "tests/agent_shim/test_agent_shim_transaction.py",
)


def _unsupported_collection_paths(platform: str) -> tuple[str, ...]:
    """Return exact lifecycle files that cannot run on the selected platform.

    Example:
        >>> _unsupported_collection_paths("win32")[0]
        'scripts/_agent_shim_posix.py'
    """
    return _WINDOWS_UNSUPPORTED_PATHS if platform == "win32" else ()


collect_ignore = list(_unsupported_collection_paths(sys.platform))

_PLUGIN_DIR = Path(__file__).resolve().parent
for _MODULE_DIR in (_PLUGIN_DIR / "scripts", _PLUGIN_DIR / "runtime" / "calibration"):
    if str(_MODULE_DIR) not in sys.path:
        sys.path.insert(0, str(_MODULE_DIR))

_CANONICAL_SCRIPT_MODULES = (
    "generate_roles",
    "_agent_shim_approval",
    "_agent_shim_journal",
    "_agent_shim_lifecycle",
    "_agent_shim_observe",
    "_agent_shim_plan",
    "_agent_shim_posix",
    "_agent_shim_transaction",
    "manage_role_agents",
)


@pytest.fixture(name="isolate_canonical_script_modules", autouse=True)
def _isolate_canonical_script_modules() -> Iterator[None]:
    """Keep test-loaded script classes isolated from doctest collection modules."""
    saved = {name: sys.modules[name] for name in _CANONICAL_SCRIPT_MODULES if name in sys.modules}
    for name in _CANONICAL_SCRIPT_MODULES:
        sys.modules.pop(name, None)

    try:
        yield
    finally:
        for name in _CANONICAL_SCRIPT_MODULES:
            sys.modules.pop(name, None)
        sys.modules.update(saved)
