"""Bound auxiliary work and serialize reasoning-cycle append operations."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _modules import load_module_registered

HELPER = Path(__file__).resolve().parents[3] / "shared" / "escalation_ledger.py"


@pytest.fixture
def ledger_module() -> ModuleType:
    """Load the standalone escalation helper."""
    return load_module_registered("escalation_bounds", HELPER)


def cycle(index: int, primary_progress: bool = False) -> dict[str, Any]:
    """Build a valid auxiliary cycle or an evidenced primary progress cycle."""
    return {
        "index": index,
        "objective": "verify the fix",
        "operation": "prepare evidence",
        "outcome": "more work remains",
        "next_decision": "continue",
        "material_progress": primary_progress,
        "work_kind": "primary" if primary_progress else "auxiliary",
        "required_for": "verification",
        "evidence": ["acceptance improved"] if primary_progress else [],
    }


def ledger(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a current working ledger with the supplied cycle history."""
    return {
        "schema_version": 3,
        "primary_goal": "repair the behavior",
        "workstream_id": "bounded-work",
        "closure_condition": {"id": "acceptance", "status": "open"},
        "outcome": "working",
        "cycles": cycles,
    }


def stage_append(ledger_module: ModuleType, header: Path, payload: dict[str, Any], record: dict[str, Any]) -> Path:
    """Write an existing ledger and stage the next observed cycle."""
    header.write_text(json.dumps({key: value for key, value in payload.items() if key != "cycles"}), encoding="utf-8")
    log = ledger_module.cycles_path(header)
    log.write_text("".join(json.dumps(item) + "\n" for item in payload["cycles"]), encoding="utf-8")
    log.with_name(log.name + ".rec").write_text(json.dumps(record), encoding="utf-8")
    return log


def test_four_auxiliary_cycles_require_escalation(ledger_module: ModuleType, tmp_path: Path) -> None:
    """Auxiliary-only work cannot keep an open workstream working indefinitely."""
    header = tmp_path / "reasoning-progress.json"
    log = stage_append(ledger_module, header, ledger([cycle(1), cycle(2), cycle(3)]), cycle(4))
    original = log.read_bytes()
    with pytest.raises(ValueError, match="auxiliary-cycle-budget-exceeded"):
        ledger_module.append_cycle(header)
    assert log.read_bytes() == original
    assert log.with_name(log.name + ".rec").is_file()


def test_primary_progress_resets_auxiliary_budget(ledger_module: ModuleType) -> None:
    """Verified primary progress admits a fresh bounded setup sequence."""
    payload = ledger([cycle(1), cycle(2), cycle(3), cycle(4, True), cycle(5), cycle(6), cycle(7)])
    ledger_module.validate_ledger(payload)


@pytest.mark.parametrize("outcome", ["closed", "advisory"])
def test_header_state_cannot_bypass_auxiliary_cap(ledger_module: ModuleType, tmp_path: Path, outcome: str) -> None:
    """A header state change does not erase an exhausted cycle budget."""
    payload = ledger([cycle(1), cycle(2), cycle(3)])
    payload["outcome"] = outcome
    payload["closure_condition"]["status"] = "closed" if outcome == "closed" else "open"
    header = tmp_path / "reasoning-progress.json"
    log = stage_append(ledger_module, header, payload, cycle(4))
    original = log.read_bytes()
    with pytest.raises(ValueError, match="auxiliary-cycle-budget-exceeded"):
        ledger_module.append_cycle(header)
    assert log.read_bytes() == original


def test_append_refuses_an_existing_writer_lock(ledger_module: ModuleType, tmp_path: Path) -> None:
    """A concurrent writer cannot validate and append against the same history."""
    header = tmp_path / "reasoning-progress.json"
    payload = ledger([])
    del payload["cycles"]
    header.write_text(json.dumps(payload), encoding="utf-8")
    log = ledger_module.cycles_path(header)
    staged = log.with_name(log.name + ".rec")
    staged.write_text(json.dumps(cycle(1)), encoding="utf-8")
    lock = log.with_name(log.name + ".lock")
    lock.write_text("another writer", encoding="utf-8")
    with pytest.raises(ValueError, match="append-lock-busy"):
        ledger_module.append_cycle(header)
    assert not log.exists()
    assert staged.is_file()
    assert lock.read_text(encoding="utf-8") == "another writer"


def test_append_releases_lock_and_consumes_staged_record(ledger_module: ModuleType, tmp_path: Path) -> None:
    """A completed append leaves one record and permits the next writer."""
    header = tmp_path / "reasoning-progress.json"
    payload = ledger([])
    del payload["cycles"]
    header.write_text(json.dumps(payload), encoding="utf-8")
    log = ledger_module.cycles_path(header)
    staged = log.with_name(log.name + ".rec")
    staged.write_text(json.dumps(cycle(1)), encoding="utf-8")
    ledger_module.append_cycle(header)
    assert json.loads(log.read_text(encoding="utf-8"))["index"] == 1
    assert not staged.exists()
    assert not log.with_name(log.name + ".lock").exists()


@pytest.mark.parametrize("historical", [False, True])
def test_existing_ledgers_keep_long_auxiliary_history(ledger_module: ModuleType, historical: bool) -> None:
    """Read existing current ledgers and historical archives without retroactive append limits."""
    payload = ledger([cycle(index) for index in range(1, 5)])
    payload["schema_version"] = 2 if historical else 3
    ledger_module.validate_ledger(payload, historical=historical)


def test_primary_progress_can_resume_after_old_budget_exhaustion(ledger_module: ModuleType, tmp_path: Path) -> None:
    """A formerly valid history cannot block recording new primary progress or a fresh auxiliary cycle."""
    header = tmp_path / "reasoning-progress.json"
    log = stage_append(ledger_module, header, ledger([cycle(index) for index in range(1, 5)]), cycle(5, True))
    ledger_module.append_cycle(header)
    log.with_name(log.name + ".rec").write_text(json.dumps(cycle(6)), encoding="utf-8")
    ledger_module.append_cycle(header)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 6
    ledger_module.validate_ledger(ledger_module.load_ledger(header))


def test_stale_lock_reports_manual_recovery(ledger_module: ModuleType, tmp_path: Path) -> None:
    """Keep an orphaned lock intact until its run owner verifies and removes it."""
    header = tmp_path / "reasoning-progress.json"
    log = stage_append(ledger_module, header, ledger([]), cycle(1))
    lock = log.with_name(log.name + ".lock")
    metadata = {"pid": 99999999, "created_at": 1.0}
    lock.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match=r"append-lock-busy:.*run owner.*owner is gone"):
        ledger_module.append_cycle(header)
    assert json.loads(lock.read_text(encoding="utf-8")) == metadata
    lock.unlink()
    ledger_module.append_cycle(header)
    assert json.loads(log.read_text(encoding="utf-8"))["index"] == 1


def test_append_lock_records_owner(ledger_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Persist the writer identity and creation time until lock cleanup."""
    header = tmp_path / "reasoning-progress.json"
    log = stage_append(ledger_module, header, ledger([]), cycle(1))
    lock = log.with_name(log.name + ".lock")
    original_unlink = Path.unlink
    observed: list[dict[str, Any]] = []
    started = time.time()

    def observe_unlink(path: Path, missing_ok: bool = False) -> None:
        """Read the on-disk ownership record before releasing the lock."""
        if path == lock:
            observed.append(json.loads(path.read_text(encoding="utf-8")))
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", observe_unlink)
    ledger_module.append_cycle(header)
    assert len(observed) == 1
    assert observed[0]["pid"] == os.getpid()
    assert started <= observed[0]["created_at"] <= time.time()


def test_lock_cleanup_does_not_mask_append_error(
    ledger_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve the staged-record failure when filesystem lock cleanup also fails."""
    original_unlink = Path.unlink

    def deny_lock_unlink(path: Path, missing_ok: bool = False) -> None:
        """Simulate a lock file that cannot be deleted."""
        if path.suffix == ".lock":
            raise PermissionError("lock cleanup denied")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny_lock_unlink)
    with pytest.raises(ValueError, match="append-record-missing"):
        ledger_module.append_cycle(tmp_path / "reasoning-progress.json")
