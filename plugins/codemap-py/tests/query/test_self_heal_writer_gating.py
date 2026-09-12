"""Tests that a stale-index self-heal stands down while another writer is already running.

The prompt hook starts a detached refresh on its own schedule. A query in the same turn used to spawn a second ``scan-
index`` regardless, which could only queue behind the first and then be killed at ``_HEAL_TIMEOUT_S`` — the full timeout
paid, nothing healed, and a writer killed mid-flight.

Gating is deliberately tested through the probe rather than through wall-clock timing: the window between the query's
index load and its heal spawn is short, so a timing-based assertion would be a flaky proxy for the branch these tests
pin directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import codemap_py.query as _query_mod  # noqa: E402  (needs the sys.path insert above)

maybe_self_heal = _query_mod.maybe_self_heal


@pytest.fixture(name="stale_index")
def _stale_index() -> dict:
    """Return an index dict whose recorded sha for one file disagrees with the tree."""
    return {"file_shas": {"pkg/mod.py": "0" * 40}, "modules": []}


@pytest.fixture(name="spawn_recorder")
def _spawn_recorder(monkeypatch) -> list[tuple]:
    """Record every incremental-scan spawn instead of running one."""
    calls: list[tuple] = []

    def _record(scan_index_bin, scan_root, changed_count):
        calls.append((scan_index_bin, scan_root, changed_count))
        return False

    monkeypatch.setattr(_query_mod, "_run_incremental_scan", _record)
    return calls


@pytest.fixture(name="one_changed_file")
def _one_changed_file(monkeypatch) -> None:
    """Report exactly one changed file, keeping the change set under the heal cap."""
    monkeypatch.setattr(_query_mod, "_changed_py_files", lambda index: ["pkg/mod.py"])


@pytest.mark.usefixtures("one_changed_file")
def test_skips_spawn_while_another_writer_is_active(
    stale_index: dict, spawn_recorder: list, tmp_path: Path, monkeypatch
) -> None:
    """No second scan is spawned when the gate reports a live writer.

    This is the hook-refresh collision: the detached refresh already holds the writer
    lease, so spawning here can only queue and die at the cap.
    """
    monkeypatch.setattr(_query_mod.rwgate, "writer_active", lambda path: True)

    result = maybe_self_heal(stale_index, tmp_path / "idx.json", tmp_path)

    assert spawn_recorder == []
    assert result is stale_index


@pytest.mark.usefixtures("one_changed_file")
def test_spawns_when_no_writer_is_active(stale_index: dict, spawn_recorder: list, tmp_path: Path, monkeypatch) -> None:
    """An idle gate still heals — the guard must not disable self-heal outright."""
    monkeypatch.setattr(_query_mod.rwgate, "writer_active", lambda path: False)

    maybe_self_heal(stale_index, tmp_path / "idx.json", tmp_path)

    assert len(spawn_recorder) == 1


@pytest.mark.usefixtures("one_changed_file")
def test_returns_the_stale_index_rather_than_failing(stale_index: dict, tmp_path: Path, monkeypatch) -> None:
    """Standing down answers from the current index; it never turns into a query error.

    A stale answer flagged as stale is the documented contract. Raising here would turn a routine refresh collision into
    a failed query.
    """
    monkeypatch.setattr(_query_mod.rwgate, "writer_active", lambda path: True)

    assert maybe_self_heal(stale_index, tmp_path / "idx.json", tmp_path) is stale_index


def test_gate_is_not_consulted_when_the_index_is_fresh(tmp_path: Path, monkeypatch) -> None:
    """A fresh index returns before the gate — no probe cost on the common path.

    Nearly every query runs against a fresh index, so the probe must sit after the change-set check, not before it.
    """
    probed: list[Path] = []
    monkeypatch.setattr(_query_mod, "_changed_py_files", lambda index: [])
    monkeypatch.setattr(_query_mod.rwgate, "writer_active", lambda path: probed.append(path) or False)

    maybe_self_heal({"file_shas": {}}, tmp_path / "idx.json", tmp_path)

    assert probed == []


def test_gate_is_not_consulted_when_the_change_set_exceeds_the_cap(tmp_path: Path, monkeypatch) -> None:
    """An oversized change set is refused before the gate, as it was before gating existed."""
    probed: list[Path] = []
    oversized = [f"pkg/mod_{n}.py" for n in range(_query_mod._HEAL_MAX_CHANGED_FILES + 1)]
    monkeypatch.setattr(_query_mod, "_changed_py_files", lambda index: oversized)
    monkeypatch.setattr(_query_mod.rwgate, "writer_active", lambda path: probed.append(path) or False)

    maybe_self_heal({"file_shas": {}}, tmp_path / "idx.json", tmp_path)

    assert probed == []
