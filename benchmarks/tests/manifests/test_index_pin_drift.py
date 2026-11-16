"""Fail when a tracked benchmark index pin names a scanner generation other than the live one.

The frozen benchmark indexes are locked by generation (``scan_version``) and by hash in two tracked places: the
methodology builder's ``INDEX_LOCK`` and ``benchmarks/suites/patch-index-locks.json``. Every generated manifest derives
its index contract from those two sources, and ``prepare-codex-index.py`` rejects a contract whose generation differs
from ``SCAN_VERSION`` in the scanner's ``schema.py``. A scanner generation bump without a relock therefore breaks every
benchmark preflight at run time; this test moves that failure to the test suite, next to the change that caused it.
"""

from __future__ import annotations

import ast
import json
import runpy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
BENCHMARKS = ROOT / "benchmarks"
SCHEMA_PATH = ROOT / "plugins" / "codemap-py" / "src" / "codemap_py" / "schema.py"
METHODOLOGY_BUILDER = BENCHMARKS / "build-provider-parity-methodology-manifest.py"
PATCH_INDEX_LOCKS = BENCHMARKS / "suites" / "patch-index-locks.json"


def _live_scan_version(schema_path: Path) -> int:
    """Read the module-level integer ``SCAN_VERSION`` from the scanner schema source without importing it.

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as directory:
    ...     path = Path(directory) / "schema.py"
    ...     _ = path.write_text("SCAN_VERSION: int = 7\\n", encoding="utf-8")
    ...     _live_scan_version(path)
    7
    """
    tree = ast.parse(schema_path.read_text(encoding="utf-8"), filename=str(schema_path))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if name == "SCAN_VERSION" and isinstance(value, ast.Constant) and type(value.value) is int:
            return value.value
    raise AssertionError(f"no integer SCAN_VERSION assignment in {schema_path}")


def _patch_lock_tasks() -> dict[str, dict[str, Any]]:
    """Return the per-task index locks of the historical patch suite."""
    payload = json.loads(PATCH_INDEX_LOCKS.read_text(encoding="utf-8"))
    tasks = payload["tasks"]
    assert isinstance(tasks, dict)
    return tasks


def _inventoried_pins() -> list[Any]:
    """Collect every tracked literal generation pin as one ``pytest.param`` per pin."""
    builder_lock = runpy.run_path(str(METHODOLOGY_BUILDER))["INDEX_LOCK"]
    pins = [pytest.param(builder_lock["scan_version"], id="methodology-builder-INDEX_LOCK")]
    pins.extend(
        pytest.param(lock["scan_version"], id=f"patch-index-locks-{task_id}")
        for task_id, lock in sorted(_patch_lock_tasks().items())
    )
    return pins


def test_patch_lock_inventory_is_not_empty() -> None:
    """The patch lock file still names its historical tasks, so the parametrized drift check below is not vacuous."""
    assert _patch_lock_tasks(), f"{PATCH_INDEX_LOCKS} lists no patch tasks"


@pytest.mark.parametrize("pinned_scan_version", _inventoried_pins())
def test_index_pin_matches_live_scan_version(pinned_scan_version: int) -> None:
    """Each tracked index pin equals the live scanner ``SCAN_VERSION``.

    A failure means the scanner generation changed without a relock: rebuild each frozen index at its pinned commit
    with the current scanner, capture its raw and semantic hashes with ``prepare-codex-index.py``, and update the pins
    together with this generation.
    """
    live = _live_scan_version(SCHEMA_PATH)
    assert type(pinned_scan_version) is int
    assert pinned_scan_version == live, (
        f"benchmark index pin scan_version {pinned_scan_version} differs from live SCAN_VERSION {live}; relock the pins"
    )
