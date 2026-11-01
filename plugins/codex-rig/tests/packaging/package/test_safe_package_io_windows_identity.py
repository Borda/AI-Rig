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

import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from _modules import load_module_registered

_MODULE_PATH = Path(__file__).resolve().parents[3] / "scripts" / "_safe_package_io.py"
module = load_module_registered("codex_rig_safe_package_io_windows_probe", _MODULE_PATH)

_UNCHANGED_IDENTITY = (16, 1, 111, 222)
_REPLACED_IDENTITY = (16, 1, 999, 222)
_PAYLOAD = b'{"ok": true}\n'


class SimulatedOperation(NamedTuple):
    """One Windows package operation driven with faked Win32 handles; only the parent recheck identity varies."""

    call: Callable[[tuple[int, ...]], object]
    expected: object
    error: str


def _install_win32_doubles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recheck_identity: tuple[int, ...]) -> None:
    """Fake the Win32-only directory primitives so the ancestor recheck sees ``recheck_identity``."""
    monkeypatch.setattr(module, "_windows_directories", lambda *args, **kwargs: ((tmp_path, _UNCHANGED_IDENTITY),))
    monkeypatch.setattr(module, "_windows_directory_identity", lambda *args, **kwargs: recheck_identity)
    monkeypatch.setattr(module, "_close_handle", lambda handle: None, raising=False)


@pytest.fixture(params=["read", "inventory"])
def windows_operation(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Provide the real ``_read_windows`` and ``_inventory_windows`` against a one-file package, one per run."""
    if request.param == "read":
        (tmp_path / "payload.json").write_bytes(_PAYLOAD)
        file_identity = (0, 1, 0, 1, 0, len(_PAYLOAD), 1, 0, 10)

        def call(recheck_identity: tuple[int, ...]) -> bytes:
            """Read the payload through the simulated Windows path."""
            _install_win32_doubles(monkeypatch, tmp_path, recheck_identity)
            monkeypatch.setattr(
                module, "_open_windows", lambda path, *, directory: -10 if directory else os.open(path, os.O_RDONLY)
            )
            monkeypatch.setattr(module, "_windows_identity", lambda *args, **kwargs: file_identity)
            fake_msvcrt = SimpleNamespace(open_osfhandle=lambda handle, flags: handle, get_osfhandle=lambda fd: fd)
            monkeypatch.setattr(module, "msvcrt", fake_msvcrt, raising=False)
            return module._read_windows(tmp_path, "payload.json", 1_000).payload

        return SimulatedOperation(call, _PAYLOAD, "package path changed during read")

    (tmp_path / "a.txt").write_bytes(b"x")

    def call(recheck_identity: tuple[int, ...]) -> tuple[str, ...]:
        """Inventory the package through the simulated Windows path."""
        _install_win32_doubles(monkeypatch, tmp_path, recheck_identity)
        monkeypatch.setattr(module, "_open_windows", lambda path, *, directory: -10)
        return module._inventory_windows(tmp_path, frozenset(), frozenset())

    return SimulatedOperation(call, ("a.txt",), "package path changed during inventory")


class TestWindowsAncestorIdentity:
    """Ancestor directories are re-checked after a Windows read or inventory by volume and file-index identity."""

    def test_accepts_unrelated_ancestor_metadata_churn(self, windows_operation: SimulatedOperation) -> None:
        """A parent re-opened with the same volume/file-index identity still lets the read or inventory pass.

        Sibling entries added or removed under the ancestor change its write time but not its identity; counting that as
        tampering was the false positive under parallel test runs.
        """
        assert windows_operation.call(_UNCHANGED_IDENTITY) == windows_operation.expected

    def test_rejects_genuine_ancestor_replacement(self, windows_operation: SimulatedOperation) -> None:
        """A parent whose file-index changed between snapshot and recheck still raises for read and inventory.

        A directory swapped underneath the operation is the real attack the recheck exists for, so the relaxed
        comparison must not hide it.
        """
        with pytest.raises(module.SafePackageIOError, match=windows_operation.error):
            windows_operation.call(_REPLACED_IDENTITY)
