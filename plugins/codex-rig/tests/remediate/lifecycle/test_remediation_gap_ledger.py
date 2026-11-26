"""Promoting a remediation result records the run's gaps in its gap ledger without ever blocking delivery.

``remediation_finalize.py finalize --promote`` calls the shared gap-record module once the result is promoted. A
recorded ledger validates under the schema owner and names the remediation run with route ``none``; a recorder failure
is reported in the summary's ``gap_ledger`` entry while the promoted result and exit status stand.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_scoped
from test_remediation_finalize import HELPER, _assert_promoted_for_owner, _finalize_judgement_drafts

#: Per-command author identity, so a runner without a global Git identity can commit; global config stays untouched.
GIT_IDENTITY = (
    "-c",
    "user.name=Gap Fixture",
    "-c",
    "user.email=gap-fixture@example.invalid",
    "-c",
    "commit.gpgsign=false",
)


@pytest.fixture
def gap_records(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Register the shipped gap-record module under the name the finalizer imports."""
    return load_module_scoped("run_gap_records", HELPER.SHARED_DIRECTORY / "run_gap_records.py", monkeypatch)


def _checkout(root: Path) -> tuple[Path, str]:
    """Create a Git checkout with a hosted origin remote and one commit; return it with its head."""
    root.mkdir()
    for arguments in (
        ("init", "-q"),
        ("remote", "add", "origin", "https://github.com/octo/demo.git"),
        ("commit", "-q", "--allow-empty", "-m", "base"),
    ):
        subprocess.run(["git", *GIT_IDENTITY, "-C", str(root), *arguments], check=True, capture_output=True)
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return root, head


def test_promotion_records_the_remediation_gaps(
    valid_run: tuple[Path, dict[str, object]],
    gap_records: ModuleType,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A promoted remediation writes a valid ledger naming its subject, run kind and route alongside the result."""
    run, result = valid_run
    checkout, head = _checkout(tmp_path_factory.mktemp("caller") / "demo")
    monkeypatch.setattr(gap_records, "checkout_root", lambda _run: checkout)

    exit_code, summary = _finalize_judgement_drafts(run, result, tmp_path_factory.mktemp("drafts"), "user", capsys)

    _assert_promoted_for_owner(run, exit_code, summary, "user")
    recorded = summary["gap_ledger"]
    assert (recorded["status"], recorded["route"], recorded["dropped"]) == ("recorded", "none", []), recorded
    ledger = gap_records.gap_ledger.load_ledger(run / "gaps.jsonl")
    assert ledger.errors == []
    assert len(ledger.records) == recorded["appended"] >= 1
    for record in ledger.records:
        assert record["subject"] == {"repo": "octo/demo", "pr": None, "head": head}
        assert (record["run"]["kind"], record["run"]["route"], record["run"]["harness"]) == (
            "remediate",
            "none",
            "codex",
        )


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        pytest.param(
            "raise", {"status": "failed", "code": "gap-ledger-unavailable:RuntimeError"}, id="recorder-raises"
        ),
        pytest.param(None, {"status": "skipped", "code": "subject-unknown"}, id="subject-unknown"),
    ],
)
def test_recorder_failure_never_blocks_promotion(
    valid_run: tuple[Path, dict[str, object]],
    gap_records: ModuleType,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    patch: str | None,
    expected: dict[str, str],
) -> None:
    """A failing or skipped recorder is reported in ``gap_ledger`` while the result stays promoted and passing."""
    run, result = valid_run
    if patch == "raise":

        def broken(_run: Path, _skill: str) -> dict[str, object]:
            """Fail the way an unexpected defect in the recorder would."""
            raise RuntimeError("defect")

        monkeypatch.setattr(gap_records, "record_promoted_run", broken)

    exit_code, summary = _finalize_judgement_drafts(run, result, tmp_path_factory.mktemp("drafts"), "user", capsys)

    _assert_promoted_for_owner(run, exit_code, summary, "user")
    assert summary["gap_ledger"] == expected
    assert (run / "result.json").is_file()
    assert not (run / "gaps.jsonl").exists()
