"""Exercise malformed hook events and symlink-resistant exhausted-query recording."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _host_skips import skip_file_symlink_unavailable

_HOOK = Path(__file__).resolve().parents[2] / "hooks" / "record-exhausted.py"
_EVENT = {
    "session_id": "sentinel-safety",
    "tool_name": "Bash",
    "tool_input": {"command": "scan-query rdeps pkg.module"},
    "tool_response": {"module": "pkg.module", "index": {"query_complete": True}},
}


def _drive(event: dict, directory: Path) -> subprocess.CompletedProcess[str]:
    """Run the real hook with a private temporary directory and captured diagnostics."""
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env={**os.environ, "TMPDIR": str(directory), "CODEMAP_RUNTIME": "claude"},
        check=False,
    )


@pytest.fixture
def hook() -> ModuleType:
    """Load the recording hook for filesystem capability and race exercises."""
    spec = importlib.util.spec_from_file_location("record_exhausted_safety", _HOOK)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("tool_name", ["Bash", "Edit"])
@pytest.mark.parametrize(
    "tool_input", [None, "bad", 7, True, pytest.param(["bad"], id="list"), pytest.param([], id="empty-list")]
)
def test_malformed_tool_input_fails_open(tmp_path: Path, tool_name: str, tool_input: object) -> None:
    """Malformed inputs never crash; unreadable edit targets still invalidate caller state."""
    sentinel = tmp_path / "codemap-exhausted-sentinel-safety"
    sentinel.write_text("pkg.module\n", encoding="utf-8")

    result = _drive({**_EVENT, "tool_name": tool_name, "tool_input": tool_input}, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == ""
    assert sentinel.exists() == (tool_name == "Bash")


@skip_file_symlink_unavailable
@pytest.mark.parametrize("target_exists", [True, False])
def test_recording_does_not_write_through_sentinel_symlink(tmp_path: Path, target_exists: bool) -> None:
    """A planted link neither appends to an existing target nor creates a dangling target."""
    target = tmp_path / ("victim" if target_exists else "missing")
    (tmp_path / "victim").write_text("untouched\n", encoding="utf-8")
    sentinel = tmp_path / "codemap-exhausted-sentinel-safety"
    sentinel.symlink_to(target)
    result = _drive(_EVENT, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert sentinel.is_symlink()
    assert target.exists() == target_exists
    assert not target_exists or target.read_text(encoding="utf-8") == "untouched\n"


def test_recording_without_nofollow_preserves_previous_modules(
    hook: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Platforms without O_NOFOLLOW can create and safely append to an ordinary sentinel."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    sentinel = tmp_path / "sentinel"

    hook.record(sentinel, _EVENT)
    hook.record(sentinel, _EVENT)

    assert sentinel.read_text(encoding="utf-8") == "pkg.module\npkg/module\npkg.module\npkg/module\n"


@skip_file_symlink_unavailable
def test_recording_rejects_dangling_link_before_exclusive_open(
    hook: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exclusive creation that follows a dangling link cannot create or append to its target."""
    sentinel = tmp_path / "sentinel"
    victim = tmp_path / "missing"
    sentinel.symlink_to(victim)
    original_open = os.open

    def follow_dangling(path: Path, flags: int, mode: int = 0o777) -> int:
        """Model Windows exclusive creation through a dangling file symlink."""
        return original_open(victim if path == sentinel else path, flags, mode)

    monkeypatch.setattr(os, "open", follow_dangling)

    hook.record(sentinel, _EVENT)

    assert sentinel.is_symlink()
    assert not victim.exists()


@skip_file_symlink_unavailable
def test_swapped_symlink_is_not_written_without_nofollow(
    hook: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sentinel swapped after inspection cannot redirect a write on the portable fallback."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("pkg.previous\n", encoding="utf-8")
    victim = tmp_path / "victim"
    victim.write_text("untouched\n", encoding="utf-8")
    original_open = os.open

    def swap_before_open(path: Path, flags: int, mode: int = 0o777) -> int:
        """Replace the checked sentinel just before the append descriptor is opened."""
        if path == sentinel and not flags & os.O_CREAT:
            sentinel.unlink()
            sentinel.symlink_to(victim)
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", swap_before_open)

    hook.record(sentinel, _EVENT)

    assert sentinel.is_symlink()
    assert victim.read_text(encoding="utf-8") == "untouched\n"


@skip_file_symlink_unavailable
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="host does not expose FIFO creation")
def test_swapped_fifo_does_not_block_recording(tmp_path: Path) -> None:
    """A raced FIFO target cannot hang the hook before its identity check."""
    sentinel = tmp_path / "codemap-exhausted-sentinel-safety"
    sentinel.write_text("pkg.previous\n", encoding="utf-8")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    script = """
import os
import runpy
import sys
from pathlib import Path

hook = runpy.run_path(sys.argv[1])
sentinel, fifo = map(Path, sys.argv[2:])
original_open = os.open
def swap(path, flags, mode=0o777):
    if not flags & os.O_CREAT:
        sentinel.unlink()
        sentinel.symlink_to(fifo)
    return original_open(path, flags, mode)
os.open = swap
os.__dict__.pop("O_NOFOLLOW", None)
raise SystemExit(hook["main"]())
"""

    result = subprocess.run(
        [sys.executable, "-c", script, str(_HOOK), str(sentinel), str(fifo)],
        input=json.dumps(_EVENT),
        text=True,
        capture_output=True,
        timeout=3,
        env={**os.environ, "TMPDIR": str(tmp_path), "CODEMAP_RUNTIME": "claude"},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert sentinel.is_symlink()
