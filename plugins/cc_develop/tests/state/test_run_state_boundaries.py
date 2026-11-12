"""Check run-state paths and plan sentinel trust across working directories."""

import os
from pathlib import Path

import dev_run_dir
import plan_hash
import pytest


def test_persisted_run_survives_working_directory_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Use one absolute run directory after entering a different worktree."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "worktree")
    assert dev_run_dir.main(["--persist", "fix"]) == 0
    run = Path(capsys.readouterr().out.strip())
    recorded = dev_run_dir.read_sentinel("dev-fix-dev-dir")
    alternate = tmp_path / "worktree"
    alternate.mkdir()
    monkeypatch.chdir(alternate)
    assert run.is_absolute()
    assert Path(recorded) == run
    assert Path(recorded).is_dir()
    assert not (alternate / ".developments").exists()


def test_persist_normalizes_relative_run_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct persistence uses the originating cwd rather than the later reader's cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "persist")
    assert dev_run_dir.persist_dev_dir("feature", Path(".developments/run")) is not None
    assert dev_run_dir.read_sentinel("dev-feature-dev-dir") == (tmp_path / ".developments/run").as_posix()


def test_plan_check_ignores_foreign_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unowned plan sentinel cannot inject a changed-plan warning."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "foreign")
    planted = dev_run_dir.sentinel_path("dev-plan-sha")
    planted.write_text("0" * 64 + "  missing.md\nINJECTED\n", encoding="utf-8")
    monkeypatch.setattr(os, "getuid", lambda: planted.stat().st_uid + 1, raising=False)
    plan_hash.check()
    assert capsys.readouterr().out == ""


def test_plan_check_uses_only_first_sentinel_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Extra sentinel lines never become agent-visible warning text."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "first-line")
    dev_run_dir.sentinel_path("dev-plan-sha").write_text("0" * 64 + "  missing.md\nINJECTED\n", encoding="utf-8")
    plan_hash.check()
    assert capsys.readouterr().out == "! BLOCKED — plan changed during build: missing.md\n"


def test_plan_record_sanitizes_session_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan hashing names session files by the same sanitization as the shared state helper."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "raw.session")
    plan = tmp_path / "plan.md"
    plan.write_text("steps", encoding="utf-8")
    plan_hash.record(str(plan))
    assert dev_run_dir.sentinel_path("dev-plan-sha").is_file()
    assert not (tmp_path / "dev-plan-sha-raw.session").exists()
