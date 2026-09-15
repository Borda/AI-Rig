"""Cross-host regression coverage for the Windows ancestor-identity comparison in ``_safe_package_io``.

``_read_windows``/``_inventory_windows`` re-check every ancestor directory's identity after reading, to catch a
directory swapped underneath the read (see the module's own threat-model docstring). Runs on every host — not gated
by ``sys.platform`` — by mocking the ctypes/msvcrt-only Win32 primitives, the same technique used to falsify the
original defect in ``.reports/codex/investigate/2026-09-15T06-10-17Z/probe.py``: only the Win32 boundary is faked,
the comparison logic under test is plain host-portable Python.

Regression for the false positive fixed 2026-09-16: an ancestor directory's ``write_time`` changes whenever any
sibling entry is added or removed (routine under parallel test runs), and used to be compared as part of the
directory's full identity tuple, tripping "package path changed" with no real tampering.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "_safe_package_io.py"
_spec = importlib.util.spec_from_file_location("codex_rig_safe_package_io_windows_probe", _MODULE_PATH)
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)

_UNCHANGED_IDENTITY = (16, 1, 111, 222)
_REPLACED_IDENTITY = (16, 1, 999, 222)


def _read_with_simulated_windows(tmp_path: Path, *, recheck_identity: tuple[int, ...]) -> bytes:
    """Drive the real ``_read_windows`` with faked Win32 handles; only the parent recheck identity varies."""
    payload = b'{"ok": true}\n'
    file_identity = (0, 1, 0, 1, 0, len(payload), 1, 0, 10)

    def fake_open_windows(path: Path, *, directory: bool) -> int:
        return -10 if directory else os.open(path, os.O_RDONLY)

    with (
        patch.object(module, "_windows_directories", return_value=((tmp_path, _UNCHANGED_IDENTITY),)),
        patch.object(module, "_open_windows", side_effect=fake_open_windows),
        patch.object(module, "_windows_identity", return_value=file_identity),
        patch.object(module, "_windows_directory_identity", return_value=recheck_identity),
        patch.object(module, "_close_handle", lambda handle: None, create=True),
        patch.object(
            module,
            "msvcrt",
            SimpleNamespace(open_osfhandle=lambda handle, flags: handle, get_osfhandle=lambda fd: fd),
            create=True,
        ),
    ):
        return module._read_windows(tmp_path, "payload.json", 1_000).payload


def test_read_accepts_unrelated_ancestor_metadata_churn(tmp_path: Path) -> None:
    """A parent directory re-opened with the same volume/file-index identity still passes the read."""
    payload = b'{"ok": true}\n'
    (tmp_path / "payload.json").write_bytes(payload)
    assert _read_with_simulated_windows(tmp_path, recheck_identity=_UNCHANGED_IDENTITY) == payload


def test_read_rejects_genuine_ancestor_replacement(tmp_path: Path) -> None:
    """A parent directory whose file-index changed between snapshot and recheck still raises."""
    (tmp_path / "payload.json").write_bytes(b'{"ok": true}\n')
    with pytest.raises(module.SafePackageIOError, match="package path changed during read"):
        _read_with_simulated_windows(tmp_path, recheck_identity=_REPLACED_IDENTITY)


def _inventory_with_simulated_windows(tmp_path: Path, *, recheck_identity: tuple[int, ...]) -> tuple[str, ...]:
    """Drive the real ``_inventory_windows`` with faked Win32 handles; only the parent recheck identity varies."""
    with (
        patch.object(module, "_windows_directories", return_value=((tmp_path, _UNCHANGED_IDENTITY),)),
        patch.object(module, "_open_windows", side_effect=lambda path, *, directory: -10),
        patch.object(module, "_windows_directory_identity", return_value=recheck_identity),
        patch.object(module, "_close_handle", lambda handle: None, create=True),
    ):
        return module._inventory_windows(tmp_path, frozenset(), frozenset())


def test_inventory_accepts_unrelated_ancestor_metadata_churn(tmp_path: Path) -> None:
    """Unrelated ancestor metadata churn does not fail a package inventory scan."""
    (tmp_path / "a.txt").write_bytes(b"x")
    assert _inventory_with_simulated_windows(tmp_path, recheck_identity=_UNCHANGED_IDENTITY) == ("a.txt",)


def test_inventory_rejects_genuine_ancestor_replacement(tmp_path: Path) -> None:
    """A genuinely replaced ancestor directory still fails a package inventory scan."""
    (tmp_path / "a.txt").write_bytes(b"x")
    with pytest.raises(module.SafePackageIOError, match="package path changed during inventory"):
        _inventory_with_simulated_windows(tmp_path, recheck_identity=_REPLACED_IDENTITY)
