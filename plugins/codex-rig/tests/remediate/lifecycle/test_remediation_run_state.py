"""Every remediation run leaves a run-state stub, so a run that stops before promotion is counted as incomplete.

``remediation_finalize.py start`` writes ``run-state.json`` with ``remediation_status`` ``incomplete``, the furthest
checkpoint reached and a cause, in the same file and shape family Code Review uses. The stub only moves forward, and
promotion removes it, so a promoted ``result.json`` never sits beside a stub saying incomplete.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_remediation_finalize import (
    HELPER,
    _assert_promoted_for_owner,
    _draft_handoff,
    _finalize_judgement_drafts,
    _stale_metadata,
)
from test_remediation_preflight import _current_inventory, _missing_proposal


def _start(run: Path, capsys: pytest.CaptureFixture[str], *checkpoint: str) -> tuple[int, dict[str, object]]:
    """Run the ``start`` action and return its exit status and printed summary."""
    exit_code = HELPER.main(["start", "--run", str(run), *checkpoint])
    return exit_code, json.loads(capsys.readouterr().out)


def _stub(run: Path) -> dict[str, object]:
    """Read the run's stub."""
    return json.loads((run / "run-state.json").read_text(encoding="utf-8"))


def test_start_writes_an_incomplete_stub(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A freshly allocated run gets an incomplete stub naming its checkpoint and cause."""
    exit_code, summary = _start(tmp_path, capsys)

    assert (exit_code, summary["state"]) == (0, "incomplete")
    assert _stub(tmp_path) == {
        "schema_version": 1,
        "remediation_status": "incomplete",
        "checkpoint": "allocated",
        "cause": "The run reached allocated and no promoted result.json was written afterwards.",
    }


def test_interrupted_run_keeps_its_furthest_checkpoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A later checkpoint advances the stub; an earlier one never moves it back, and nothing marks it complete."""
    _start(tmp_path, capsys, "--checkpoint", "implementation")

    exit_code, summary = _start(tmp_path, capsys, "--checkpoint", "selection")

    assert (exit_code, summary["checkpoint"]) == (0, "implementation")
    assert _stub(tmp_path)["checkpoint"] == "implementation"
    assert not (tmp_path / "result.json").exists()


def test_unknown_checkpoint_is_rejected(tmp_path: Path) -> None:
    """A checkpoint outside the remediation sequence is invalid input and writes nothing."""
    with pytest.raises(SystemExit) as raised:
        HELPER.main(["start", "--run", str(tmp_path), "--checkpoint", "../escape"])

    assert raised.value.code == 2
    assert not (tmp_path / "run-state.json").exists()


def _write_inventory(run: Path, damage=None) -> None:
    """Write a current selection inventory, optionally damaged, as the selection preflight reads it."""
    inventory = _current_inventory()
    if damage is not None:
        damage(inventory)
    (run / "selection.json").write_text(json.dumps(inventory), encoding="utf-8")


def _append_closure(run: Path, heading: str = "B1") -> int:
    """Stage and append one closure record through the public action."""
    (run / "closure-log.md.rec").write_text(
        f"### {heading}\n\nChanged: a.py. Verified: focused test.\n", encoding="utf-8"
    )
    return HELPER.main(["append", "--run", str(run), "--ledger", "closure-log.md"])


def test_passing_selection_preflight_records_its_checkpoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A run that stops while its findings await selection is counted at selection, not at allocation."""
    _start(tmp_path, capsys)
    _write_inventory(tmp_path)

    assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", "selection"]) == 0

    assert _stub(tmp_path)["checkpoint"] == "selection"


def test_failing_preflight_records_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Only a passing preflight proves its stage was reached."""
    _start(tmp_path, capsys)
    _write_inventory(tmp_path, _missing_proposal)

    assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", "selection"]) == 1

    assert _stub(tmp_path)["checkpoint"] == "allocated"


def test_closure_record_marks_implementation_and_never_moves_back(
    valid_run: tuple[Path, dict], capsys: pytest.CaptureFixture[str]
) -> None:
    """A selected bucket closure proves implementation and cannot pull a gates-run stub back."""
    run, _ = valid_run
    _start(run, capsys, "--checkpoint", "plan")

    assert _append_closure(run) == 0
    assert _stub(run)["checkpoint"] == "implementation"

    capsys.readouterr()
    _start(run, capsys, "--checkpoint", "gates-run")
    assert _append_closure(run) == 0
    assert _stub(run)["checkpoint"] == "gates-run"


@pytest.mark.parametrize("heading", ["Merge coupling", "B1"])
def test_preselection_closure_keeps_the_source_checkpoint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], heading: str
) -> None:
    """Retain preselection closure evidence without mistaking a heading for a selected bucket."""
    _start(tmp_path, capsys, "--checkpoint", "source-collected")

    assert _append_closure(tmp_path, heading) == 0

    assert _stub(tmp_path)["checkpoint"] == "source-collected"
    assert f"### {heading}" in (tmp_path / "closure-log.md").read_text(encoding="utf-8")


def test_unwritable_stub_never_fails_the_recording_action(
    valid_run: tuple[Path, dict], capsys: pytest.CaptureFixture[str]
) -> None:
    """The stub is a diagnostic: a stub path the writer cannot replace is reported and the append still lands."""
    run, _ = valid_run
    (run / "run-state.json").mkdir()

    assert _append_closure(run) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["appended"] == 1
    assert "run-state-unrecorded:implementation:" in captured.err
    assert "B1" in (run / "closure-log.md").read_text(encoding="utf-8")


def test_unremovable_stub_never_undoes_promotion(
    valid_run: tuple[Path, dict[str, object]],
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stub path promotion cannot remove is reported, and the promoted result still stands."""
    run, result = valid_run
    (run / "run-state.json").mkdir()
    drafts = tmp_path_factory.mktemp("drafts")
    (drafts / "metadata.json").write_text(json.dumps(_stale_metadata(result)), encoding="utf-8")
    (drafts / "handoff.json").write_text(json.dumps(_draft_handoff(run)), encoding="utf-8")
    argv = ["finalize", "--run", str(run), "--metadata", str(drafts / "metadata.json")]
    argv += ["--handoff", str(drafts / "handoff.json"), "--status", result["status"]]
    argv += ["--confidence", str(result["confidence"]), "--artifact-path", result["artifact_path"], "--promote"]

    exit_code = HELPER.main(argv)

    captured = capsys.readouterr()
    assert (exit_code, json.loads(captured.out)["promoted"]) == (0, True)
    assert (run / "result.json").is_file()
    assert "run-state-not-removed:" in captured.err


def test_promotion_removes_the_stub(
    valid_run: tuple[Path, dict[str, object]],
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Promotion removes the incomplete stub, and a later ``start`` never writes one beside the promoted result."""
    run, result = valid_run
    _start(run, capsys, "--checkpoint", "gates-run")

    exit_code, summary = _finalize_judgement_drafts(run, result, tmp_path_factory.mktemp("drafts"), "user", capsys)

    _assert_promoted_for_owner(run, exit_code, summary, "user")
    assert not (run / "run-state.json").exists()
    assert _start(run, capsys, "--checkpoint", "allocated")[1]["state"] == "promoted"
    assert not (run / "run-state.json").exists()
