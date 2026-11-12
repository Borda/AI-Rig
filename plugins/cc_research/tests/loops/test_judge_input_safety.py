"""Approval gates bind report selection and content to a confined source run."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import find_judge_verdict as judge
import pytest


@pytest.fixture
def approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a matching program, state and content-bound approval in an isolated project."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "approval-test")
    (tmp_path / "program.md").write_text("baseline\n", encoding="utf-8")
    state = tmp_path / "state" / "run"
    state.mkdir(parents=True)
    (state / "state.json").write_text(json.dumps({"program_file": "program.md"}), encoding="utf-8")
    (tmp_path / "reports").mkdir()
    _report(tmp_path, "judge-approved.md", "program.md", "APPROVED")
    return tmp_path


def _report(root: Path, name: str, program: str, verdict: str) -> Path:
    """Write report metadata calculated from the program bytes at judgment time."""
    digest = hashlib.sha256((root / program).read_bytes()).hexdigest()
    report = root / "reports" / name
    report.write_text(f"Program: {program}\nProgram SHA256: {digest}\nVerdict: {verdict}\n", encoding="utf-8")
    return report


def _args(root: Path, *, run_id: str = "run", state_base: Path | None = None) -> list[str]:
    """Build the CLI invocation for a private project and explicit run identity."""
    return [
        "--state-dir-base",
        str(state_base or root / "state"),
        "--run-id",
        run_id,
        "--reports-dir",
        str(root / "reports"),
    ]


def test_newer_report_for_other_program_does_not_hide_approval(approval: Path) -> None:
    """An unrelated program judgment cannot supersede this program's approval."""
    (approval / "other.md").write_text("other", encoding="utf-8")
    newer = _report(approval, "judge-other.md", "other.md", "BLOCKED")
    os.utime(approval / "reports" / "judge-approved.md", (100, 100))
    os.utime(newer, (200, 200))

    assert judge.main(_args(approval)) == 0
    assert (approval / "fortify-judge-verdict-approval-test").read_text(encoding="utf-8") == "APPROVED\n"


def test_latest_matching_rejection_supersedes_approval(approval: Path) -> None:
    """Filtering by program still honors a later rejection of that same program."""
    newer = _report(approval, "judge-rejected.md", "program.md", "BLOCKED")
    os.utime(approval / "reports" / "judge-approved.md", (100, 100))
    os.utime(newer, (200, 200))

    assert judge.main(_args(approval)) == 0
    assert (approval / "fortify-judge-verdict-approval-test").read_text(encoding="utf-8") == "BLOCKED\n"


def test_edited_program_invalidates_approval(approval: Path) -> None:
    """An approval for old bytes does not approve later edits at the same path."""
    (approval / "program.md").write_text("changed protocol\n", encoding="utf-8")

    assert judge.main(_args(approval)) == 1
    assert not (approval / "fortify-judge-verdict-approval-test").exists()


def test_report_without_digest_is_admitted_with_note(approval: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Reports predating content binding remain usable with an explicit warning."""
    (approval / "reports" / "judge-approved.md").write_text(
        "Program: program.md\nVerdict: APPROVED\n", encoding="utf-8"
    )

    assert judge.main(_args(approval)) == 0
    assert (
        "judge report predates content binding; program edits since judgment are not detected"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize("digest", ["xyz", ""])
def test_malformed_digest_blocks_approval(approval: Path, digest: str) -> None:
    """A present digest field must contain a valid hash, including when it is blank."""
    (approval / "reports" / "judge-approved.md").write_text(
        f"Program: program.md\nProgram SHA256: {digest}\nVerdict: APPROVED\n", encoding="utf-8"
    )

    assert judge.main(_args(approval)) == 1


def _symlinks_supported() -> bool:
    """Probe whether the host permits directory symlinks without elevated privileges."""
    try:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "link").symlink_to(Path(directory) / "target", target_is_directory=True)
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create directory symlinks")
def test_state_under_symlinked_experiments_is_admitted(
    approval: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """An experiments directory on another disk remains an allowed source of run state."""
    outside = tmp_path_factory.mktemp("experiments-target")
    (outside / "state").mkdir()
    (outside / "state" / "run").mkdir()
    (outside / "state" / "run" / "state.json").write_text(json.dumps({"program_file": "program.md"}), encoding="utf-8")
    (approval / ".experiments").symlink_to(outside, target_is_directory=True)

    assert judge.main(_args(approval, state_base=approval / ".experiments" / "state")) == 0


@pytest.mark.parametrize("run_id", ["../state/run", "run/../run", "", ".", ".."])
def test_invalid_run_identity_is_rejected(approval: Path, run_id: str) -> None:
    """Only a single validated run-directory name may select state."""
    assert judge.main(_args(approval, run_id=run_id)) == 2
    assert not (approval / "fortify-judge-verdict-approval-test").exists()


def test_state_base_outside_project_is_rejected(approval: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Matching out-of-tree state is insufficient to unlock a project approval."""
    outside = tmp_path_factory.mktemp("outside-state")
    (outside / "run").mkdir()
    (outside / "run" / "state.json").write_text(json.dumps({"program_file": "program.md"}), encoding="utf-8")

    assert judge.main(_args(approval, state_base=outside)) == 2
    assert not (approval / "fortify-judge-verdict-approval-test").exists()


def test_oversized_report_blocks_without_reading_whole_file(approval: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded reader rejects a report exceeding its limit without producing an approval."""
    monkeypatch.setattr(judge, "MAX_JSONL_BYTES", 256, raising=False)
    report = approval / "reports" / "judge-approved.md"
    report.write_text(report.read_text(encoding="utf-8") + "x" * 256, encoding="utf-8")

    assert judge.main(_args(approval)) == 1
    assert not (approval / "fortify-judge-verdict-approval-test").exists()


def test_oversized_state_is_unusable(approval: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """State metadata obeys the same byte ceiling as the report reader."""
    monkeypatch.setattr(judge, "MAX_JSONL_BYTES", 16, raising=False)

    assert judge.state_program_file(approval / "state" / "run" / "state.json") == ""


def test_nested_state_is_unusable(approval: Path) -> None:
    """Deep JSON nesting degrades to unusable state rather than a traceback."""
    state = approval / "state" / "run" / "state.json"
    state.write_text("[" * 10000 + "]" * 10000, encoding="utf-8")

    assert judge.state_program_file(state) == ""


def test_hash_program_prints_digest_without_resolving_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Digest-only mode works without a run, reports, writable sentinels or a valid session identity."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSID", "invalid/session")
    program = tmp_path / "program.md"
    program.write_bytes(b"program\r\n")

    assert judge.main(["--hash-program", str(program)]) == 0
    output = capsys.readouterr()
    assert output.out == hashlib.sha256(b"program\r\n").hexdigest() + "\n"
    assert output.err == ""
    assert sorted(path.name for path in tmp_path.iterdir()) == ["program.md"]


def test_hash_program_reports_missing_input_without_digest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing program is an input error and cannot produce a misleading digest."""
    assert judge.main(["--hash-program", str(tmp_path / "missing.md")]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "cannot hash program" in output.err
