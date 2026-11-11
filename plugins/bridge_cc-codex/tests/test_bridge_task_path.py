"""Exercise scratch-path allocation and bounded cleanup without a provider or project writes."""

from __future__ import annotations

import re
import runpy
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from _bridge_link_probes import directory_symlinks_supported, file_symlinks_supported

SCRIPT = Path(__file__).resolve().parents[1] / "bin/bridge_task_path.py"
_skip_no_file_symlink = pytest.mark.skipif(not file_symlinks_supported(), reason="requires file symlink creation")
_skip_no_directory_symlink = pytest.mark.skipif(
    not directory_symlinks_supported(), reason="requires directory symlink creation"
)


@pytest.fixture
def scratch_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Select a disposable temporary directory and deterministic session identity."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "session-7")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return tmp_path


def test_cli_prints_absolute_session_scoped_uuid_without_creating_file(scratch_environment: Path) -> None:
    """The Write tool receives one absolute path while allocation leaves the filesystem untouched."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--workspace", str(scratch_environment / "workspace")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    path = Path(result.stdout.strip())
    assert path.is_absolute()
    assert path.parent == scratch_environment.resolve()
    assert re.fullmatch(r"bridge-task-session-7-[0-9a-f-]{36}\.md", path.name)
    assert UUID(path.name.removeprefix("bridge-task-session-7-").removesuffix(".md")).version == 4
    assert not path.exists()


@pytest.mark.parametrize("session", ["", "claude-session"])
def test_session_fallback_uses_claude_id_then_shared(
    scratch_environment: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], session: str
) -> None:
    """An empty CSID falls through to the host session and then the shared identity."""
    monkeypatch.setenv("CSID", "")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main(["--workspace", str(scratch_environment / "workspace")]) == 0
    assert Path(capsys.readouterr().out.strip()).name.startswith(f"bridge-task-{session or 'shared'}-")


def test_missing_tmpdir_uses_platform_temp_directory(
    scratch_environment: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unset TMPDIR uses the platform resolver rather than a hardcoded POSIX directory."""
    monkeypatch.delenv("TMPDIR")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(scratch_environment))
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main(["--workspace", str(scratch_environment / "workspace")]) == 0
    assert Path(capsys.readouterr().out.strip()).parent == scratch_environment.resolve()


def test_repeated_allocation_uses_distinct_paths(scratch_environment: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two dispatches in one session must not overwrite the same input brief."""
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main(["--workspace", str(scratch_environment / "workspace")]) == 0
    first = capsys.readouterr().out
    assert main(["--workspace", str(scratch_environment / "workspace")]) == 0
    assert capsys.readouterr().out != first


@pytest.mark.parametrize("workspace", [".", "workspace/.."])
def test_temp_directory_inside_workspace_is_refused(
    scratch_environment: Path, capsys: pytest.CaptureFixture[str], workspace: str
) -> None:
    """Neither direct nor traversal spelling may put a task file in the peer's workspace."""
    (scratch_environment / "workspace").mkdir()
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main(["--workspace", str(scratch_environment / workspace)]) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "inside" in captured.err


@_skip_no_directory_symlink
def test_symlinked_temp_directory_inside_workspace_is_refused(
    scratch_environment: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A temporary-directory alias cannot conceal that the peer owns its destination."""
    workspace = scratch_environment / "workspace"
    workspace.mkdir()
    alias = scratch_environment / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    monkeypatch.setenv("TMPDIR", str(alias))
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main(["--workspace", str(workspace)]) != 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("session", ["../escape", "bad/session", "bad\\session", "bad\nline"])
def test_session_cannot_escape_or_inject_output(
    scratch_environment: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], session: str
) -> None:
    """Session metadata must remain one safe filename component and one output line."""
    monkeypatch.setenv("CSID", session)
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main(["--workspace", str(scratch_environment / "workspace")]) != 0
    assert capsys.readouterr().out == ""


def test_cleanup_deletes_only_matching_scratch_file(scratch_environment: Path) -> None:
    """The helper removes its matching temporary input without needing a shell deletion command."""
    path = scratch_environment / f"bridge-task-session-7-{uuid4()}.md"
    path.write_text("untrusted task", encoding="utf-8")
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main(["--cleanup", str(path)]) == 0
    assert not path.exists()


def test_cleanup_missing_matching_file_is_idempotent(scratch_environment: Path) -> None:
    """Best-effort cleanup may be repeated after the input was already removed."""
    path = scratch_environment / f"bridge-task-session-7-{uuid4()}.md"
    assert runpy.run_path(str(SCRIPT))["main"](["--cleanup", str(path)]) == 0


def test_cleanup_refuses_matching_name_outside_temp_directory(scratch_environment: Path) -> None:
    """A matching basename cannot authorize deletion in a caller-owned directory."""
    other = scratch_environment / "other"
    other.mkdir()
    path = other / f"bridge-task-session-7-{uuid4()}.md"
    path.write_text("retain", encoding="utf-8")
    assert runpy.run_path(str(SCRIPT))["main"](["--cleanup", str(path)]) != 0
    assert path.read_text(encoding="utf-8") == "retain"


@pytest.mark.parametrize("name", ["caller.md", "bridge-task-session-nonce.md", "bridge-task-session-../target.md"])
def test_cleanup_refuses_unrelated_filenames(scratch_environment: Path, name: str) -> None:
    """Cleanup cannot consume caller briefs or paths that merely resemble the scratch prefix."""
    path = scratch_environment / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("retain", encoding="utf-8")
    assert runpy.run_path(str(SCRIPT))["main"](["--cleanup", str(path)]) != 0
    assert path.read_text(encoding="utf-8") == "retain"


def test_cleanup_refuses_matching_directory(scratch_environment: Path) -> None:
    """The cleanup mode never removes directories, even when their names match."""
    path = scratch_environment / f"bridge-task-session-7-{uuid4()}.md"
    path.mkdir()
    assert runpy.run_path(str(SCRIPT))["main"](["--cleanup", str(path)]) != 0
    assert path.is_dir()


@_skip_no_file_symlink
def test_cleanup_refuses_symlink_and_preserves_target(scratch_environment: Path) -> None:
    """A matching link must not authorize removal of a caller's brief or its link."""
    target = scratch_environment / "caller.md"
    target.write_text("retain", encoding="utf-8")
    path = scratch_environment / f"bridge-task-session-7-{uuid4()}.md"
    path.symlink_to(target)
    assert runpy.run_path(str(SCRIPT))["main"](["--cleanup", str(path)]) != 0
    assert path.is_symlink()
    assert target.read_text(encoding="utf-8") == "retain"
