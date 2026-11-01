"""Acceptance checks for Codex Rig's platform-specific pytest collection policy."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CONFTEST_PATH = PLUGIN_ROOT / "conftest.py"


def _load_conftest() -> ModuleType:
    """Load the plugin conftest as an ordinary module for policy checks."""
    return load_module("codex_rig_collection_policy", CONFTEST_PATH)


def test_simulated_windows_ignores_only_unsupported_posix_lifecycle_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep native Windows away from exact POSIX files without hiding portable coverage.

    The conftest is loaded with `sys.platform` forced to win32 so the policy runs on every host: only the four POSIX
    lifecycle paths are ignored there, and darwin and linux ignore nothing.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    module = _load_conftest()

    expected = (
        "scripts/_agent_shim_posix.py",
        "scripts/_agent_shim_transaction.py",
        "tests/agent_shim/test_agent_shim_posix.py",
        "tests/agent_shim/test_agent_shim_transaction.py",
    )
    assert module.collect_ignore == list(expected)
    assert module._unsupported_collection_paths("win32") == expected
    assert module._unsupported_collection_paths("darwin") == ()
    assert module._unsupported_collection_paths("linux") == ()
