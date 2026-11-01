"""Load a shipped script by file path, the way a skill invokes it, without any package import.

Three variants differ only in how the loaded module is registered in ``sys.modules`` while its body executes: not at
all, permanently, or through a ``monkeypatch`` so the registration is undone when the test ends.
"""

from __future__ import annotations

import importlib.util
import sys
from importlib.abc import Loader
from pathlib import Path
from types import ModuleType

import pytest


def _new_module(name: str, path: Path) -> tuple[ModuleType, Loader]:
    """Create the not-yet-executed module for ``path`` together with the loader that runs its body."""
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    return importlib.util.module_from_spec(specification), specification.loader


def load_module(name: str, path: Path) -> ModuleType:
    """Execute the script at ``path`` as module ``name`` without registering it in ``sys.modules``.

    Args:
        name: Module name recorded on the loaded module.
        path: Script to execute.

    Returns:
        The executed module.
    """
    module, loader = _new_module(name, path)
    loader.exec_module(module)
    return module


def load_module_registered(name: str, path: Path) -> ModuleType:
    """Execute the script at ``path`` as module ``name``, leaving it in ``sys.modules`` for the rest of the process.

    Registration happens before execution because dataclass creation resolves string annotations through the defining
    module's ``sys.modules`` entry.

    Args:
        name: Module name recorded on, and used to register, the loaded module.
        path: Script to execute.

    Returns:
        The executed module.
    """
    module, loader = _new_module(name, path)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def load_module_scoped(name: str, path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Execute the script at ``path`` as module ``name``, registered in ``sys.modules`` only until ``monkeypatch``
    undoes it.

    Args:
        name: Module name recorded on, and used to register, the loaded module.
        path: Script to execute.
        monkeypatch: Patch (or ``pytest.MonkeyPatch.context()``) whose teardown removes the registration.

    Returns:
        The executed module.
    """
    module, loader = _new_module(name, path)
    monkeypatch.setitem(sys.modules, name, module)
    loader.exec_module(module)
    return module
