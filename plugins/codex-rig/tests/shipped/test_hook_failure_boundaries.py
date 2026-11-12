"""Exercise hook failures at transcript, installer, and diagnostic process boundaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _fifo_available() -> bool:
    """Probe whether this host can create a FIFO in a private temporary directory."""
    if not hasattr(os, "mkfifo"):
        return False
    try:
        with tempfile.TemporaryDirectory() as directory:
            os.mkfifo(Path(directory) / "fifo")
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _fifo_available(), reason="FIFO creation is unavailable")
def test_stop_hook_rejects_fifo_without_waiting(tmp_path: Path) -> None:
    """Return nonblocking JSON without requiring a FIFO writer."""
    transcript = tmp_path / "transcript.jsonl"
    os.mkfifo(transcript)

    completed = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "hooks" / "review_stop.py")],
        input=json.dumps({"hook_event_name": "Stop", "transcript_path": str(transcript)}),
        text=True,
        capture_output=True,
        check=False,
        timeout=3,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {}


@pytest.mark.skipif(not _fifo_available(), reason="FIFO creation is unavailable")
def test_transcript_replaced_by_fifo_is_rejected_and_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A path replacement between stat and open cannot block or leave a descriptor open."""
    hook = load_module("review_stop_replacement", PLUGIN_ROOT / "hooks" / "review_stop.py")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"{}\n")
    native_open = os.open
    descriptors: list[int] = []

    def replace_before_open(path: str, flags: int) -> int:
        """Replace the checked file at the real filesystem open boundary."""
        assert flags & os.O_NONBLOCK
        transcript.unlink()
        os.mkfifo(transcript)
        descriptor = native_open(path, flags)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(hook.os, "open", replace_before_open)

    with pytest.raises(ValueError, match="not a regular file"):
        hook._open_transcript(str(transcript), os.O_RDONLY)

    assert len(descriptors) == 1
    with pytest.raises(OSError, match=r"\[Errno 9\]"):
        os.fstat(descriptors[0])


def test_regular_transcript_remains_readable_without_nonblock_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read ordinary transcript bytes on hosts without the POSIX nonblocking flag."""
    hook = load_module("review_stop_portable_open", PLUGIN_ROOT / "hooks" / "review_stop.py")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"{}\n")
    monkeypatch.delattr(hook.os, "O_NONBLOCK", raising=False)

    descriptor = hook._open_transcript(str(transcript), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as stream:
        assert stream.read() == b"{}\n"


@pytest.fixture
def startup_package(tmp_path: Path) -> Path:
    """Prepare an isolated startup hook with a real imported parser and diagnostic subprocess."""
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copy2(PLUGIN_ROOT / "hooks" / "session_start.py", root / "hooks" / "session_start.py")
    (root / "scripts" / "manage_role_agents.py").write_text(
        'print(\'{"classification":"healthy"}\')\n', encoding="utf-8"
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_text("managed instructions", encoding="utf-8")
    return root


def _startup(root: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Run a SessionStart event through the real hook entrypoint."""
    return subprocess.run(
        [sys.executable, str(root / "hooks" / "session_start.py")],
        input=json.dumps({"hook_event_name": "SessionStart"}),
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env={**os.environ, "PLUGIN_ROOT": str(root), "CODEX_HOME": str(home)},
    )


@pytest.mark.parametrize("error", ["AttributeError", "TypeError", "KeyError"])
@pytest.mark.parametrize("stage", ["load", "body", "root"])
def test_installer_parser_errors_preserve_healthy_startup(startup_package: Path, error: str, stage: str) -> None:
    """An incompatible installer parser cannot replace a healthy doctor result or crash startup."""
    sources = {
        "load": f"raise {error}('private parser detail')\n",
        "body": f"def authenticated_managed_body(payload):\n    raise {error}('private parser detail')\n",
        "root": (
            "def authenticated_managed_body(payload):\n    return 'body'\n"
            f"def rendered_plugin_root(body):\n    raise {error}('private parser detail')\n"
        ),
    }
    (startup_package / "scripts" / "install_global_agents.py").write_text(sources[stage], encoding="utf-8")

    completed = _startup(startup_package, startup_package.parent / "home")

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {"continue": True, "suppressOutput": False}


def test_malformed_doctor_classification_returns_nonblocking_json(startup_package: Path) -> None:
    """A malformed diagnostic result cannot escape the startup presentation boundary."""
    (startup_package / "scripts" / "install_global_agents.py").write_text(
        "def authenticated_managed_body(payload):\n    return None\n", encoding="utf-8"
    )
    (startup_package / "scripts" / "manage_role_agents.py").write_text(
        "print('{\"classification\":[]}')\n", encoding="utf-8"
    )

    completed = _startup(startup_package, startup_package.parent / "home")

    assert completed.returncode == 0
    assert completed.stderr == ""
    response = json.loads(completed.stdout)
    assert response["continue"] is True
    assert "health check unavailable" in response["systemMessage"]
    assert "Run $codex-rig:agent-shims doctor" in response["systemMessage"]


@pytest.mark.parametrize("missing_root", ["/private/session-root/missing", r"C:\private\session-root\missing"])
def test_startup_errors_do_not_disclose_private_paths(tmp_path: Path, missing_root: str) -> None:
    """An unavailable plugin root yields a useful warning without POSIX or Windows private paths."""
    completed = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "hooks" / "session_start.py")],
        input=json.dumps({"hook_event_name": "SessionStart"}),
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env={**os.environ, "PLUGIN_ROOT": missing_root, "CODEX_HOME": str(tmp_path)},
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    response = json.loads(completed.stdout)
    assert response["continue"] is True
    assert "health check unavailable" in response["systemMessage"]
    assert "Run $codex-rig:agent-shims doctor" in response["systemMessage"]
    assert "session-root" not in response["systemMessage"]
    assert str(tmp_path) not in response["systemMessage"]
