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

    monkeypatch.setattr(_query_mod.index_io, "_run_incremental_scan", _record)
    return calls


@pytest.fixture(name="one_changed_file")
def _one_changed_file(monkeypatch) -> None:
    """Report exactly one changed file, keeping the change set under the heal cap."""
    monkeypatch.setattr(_query_mod.index_io, "_changed_py_files", lambda index: ["pkg/mod.py"])


@pytest.fixture(name="writer_probe")
def _writer_probe(monkeypatch) -> list[Path]:
    """Report no active writer and record every path the gate is asked about."""
    probed: list[Path] = []
    monkeypatch.setattr(_query_mod.rwgate, "writer_active", lambda path: probed.append(path) or False)
    return probed


@pytest.mark.usefixtures("one_changed_file")
class TestSelfHealWriterGating:
    """The heal spawn is gated on the writer probe, after the cheaper change-set checks."""

    def test_skips_spawn_and_returns_stale_index_while_writer_active(
        self, stale_index: dict, spawn_recorder: list, tmp_path: Path, monkeypatch
    ) -> None:
        """A live writer means no second scan is spawned and the query is answered from the current index.

        This is the hook-refresh collision: the detached refresh already holds the writer lease, so spawning here can
        only queue and die at the cap. Standing down returns the stale index flagged as stale (the documented
        contract); raising would turn a routine refresh collision into a failed query.
        """
        monkeypatch.setattr(_query_mod.rwgate, "writer_active", lambda path: True)

        result = maybe_self_heal(stale_index, tmp_path / "idx.json", tmp_path)

        assert spawn_recorder == []
        assert result is stale_index

    def test_spawns_when_no_writer_active(self, stale_index: dict, spawn_recorder: list, tmp_path: Path, writer_probe):
        """An idle gate still heals — the guard must not disable self-heal outright.

        The same stale index as the busy-writer case, but nothing holds the lease, so exactly one scan is spawned.
        """
        maybe_self_heal(stale_index, tmp_path / "idx.json", tmp_path)

        assert len(spawn_recorder) == 1


class TestGateNotConsulted:
    """Cheap early exits return before the writer probe is paid for."""

    @pytest.mark.parametrize(
        "changed",
        [
            pytest.param([], id="fresh-index"),
            pytest.param([f"pkg/mod_{n}.py" for n in range(_query_mod._HEAL_MAX_CHANGED_FILES + 1)], id="over-cap"),
        ],
    )
    def test_writer_probe_skipped(self, tmp_path: Path, monkeypatch, writer_probe: list[Path], changed: list[str]):
        """A fresh index, or a change set above the heal cap, returns before the gate is asked.

        Nearly every query runs against a fresh index, so the probe must sit after the change-set check, not before it;
        an oversized change set is refused before the gate exactly as it was before gating existed.
        """
        monkeypatch.setattr(_query_mod.index_io, "_changed_py_files", lambda index: changed)

        maybe_self_heal({"file_shas": {}, "source_fingerprint": "0" * 64}, tmp_path / "idx.json", tmp_path)

        assert writer_probe == []
