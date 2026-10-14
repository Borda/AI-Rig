"""Real child-process regressions for result delivery and optional telemetry."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BIN_ROOT = Path(__file__).resolve().parents[1] / "bin"
if str(BIN_ROOT) not in sys.path:
    sys.path.insert(0, str(BIN_ROOT))

import bridge_call  # noqa: E402


def _supports_health_link(kind: str) -> bool:
    """Probe file-link creation before collection on platforms restricting links."""
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "target"
        target.write_text("probe", encoding="utf-8")
        link = Path(directory) / "link"
        try:
            if kind == "hardlink":
                os.link(target, link)
            else:
                link.symlink_to(target)
        except OSError:
            return False
        return True


def _deliver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: dict[str, object]) -> dict[str, object]:
    """Run a deterministic local peer through the public foreground consumer."""
    payload = json.dumps({"type": "result", "is_error": False, "structured_output": core})
    source = "from pathlib import Path\n"
    for filename in core["files_touched"]:
        source += f"Path({filename!r}).write_text('value = 2\\n')\n"
    source += "with Path('child-runs.txt').open('a') as stream: stream.write('run\\n')\n"
    source += f"print({payload!r})\n"
    emitter = tmp_path / "peer.py"
    emitter.write_text(source, encoding="utf-8")
    monkeypatch.setattr(bridge_call, "build_claude_argv", lambda *_: [sys.executable, str(emitter)])
    request = bridge_call.Request(
        verb="implement",
        task="Deliver local changes.",
        model="fixture",
        effort="medium",
        timeout_seconds=10,
        depth=0,
        run_id="delivery",
        workspace=tmp_path,
        direction="codex_to_claude",
    )
    return bridge_call.run_request(request, host="claude")


def _core() -> dict[str, object]:
    """Return valid result facts shared by delivery scenarios."""
    return {
        "status": "complete",
        "verdict": "Delivered.",
        "findings": ["Verified local change."],
        "files_touched": ["delivered.py"],
        "remaining": [],
        "blockers": [],
        "details": [],
    }


@pytest.mark.integration
def test_real_child_preserves_all_result_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep nine real edits and verbose peer evidence below actual capture limits."""
    core = _core()
    core["files_touched"] = [f"changed_{index}.py" for index in range(9)]
    core["verdict"] = "Delivered " + "v" * 501
    for field in ("findings", "remaining", "blockers"):
        core[field] = [f"{index}:" + "e" * 501 for index in range(9)]
    core["details"] = [f"{index}:" + "d" * 2001 for index in range(33)]

    result = _deliver(tmp_path, monkeypatch, core)

    for field in bridge_call.CORE_FIELDS:
        assert result[field] == core[field]
    assert all((tmp_path / filename).read_text() == "value = 2\n" for filename in core["files_touched"])
    transcript = (tmp_path / result["transcript_path"]).read_text(encoding="utf-8")
    assert all(detail in transcript for detail in core["details"])
    assert "details" not in result
    assert (tmp_path / "child-runs.txt").read_text() == "run\n"
    assert bridge_call.validate_envelope(result) == result


@pytest.mark.integration
@pytest.mark.parametrize(
    "health_kind",
    [
        "safe",
        pytest.param(
            "hardlink",
            marks=pytest.mark.skipif(
                not _supports_health_link("hardlink"), reason="requires hard-link creation capability"
            ),
        ),
        pytest.param(
            "symlink",
            marks=pytest.mark.skipif(
                not _supports_health_link("symlink"), reason="requires file symlink creation capability"
            ),
        ),
        "directory",
    ],
)
@pytest.mark.parametrize("valid", [True, False])
def test_real_child_result_survives_optional_health_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, health_kind: str, valid: bool
) -> None:
    """Keep primary results and refuse unsafe telemetry without replaying the child."""
    store = tmp_path / ".temp" / "bridge"
    store.mkdir(parents=True)
    health = store / "health.jsonl"
    protected = tmp_path / "protected.txt"
    protected.write_text("unchanged\n", encoding="utf-8")
    if health_kind == "hardlink":
        os.link(protected, health)
    elif health_kind == "symlink":
        health.symlink_to(protected)
    elif health_kind == "directory":
        health.mkdir()
    core = _core()
    if not valid:
        del core["status"]

    result = _deliver(tmp_path, monkeypatch, core)

    assert result["status"] == ("complete" if valid else "blocked")
    assert result["verdict"] == (
        core["verdict"] if valid else "invalid model result: model result must contain exactly the seven peer fields"
    )
    if valid:
        for field in ("files_touched", "remaining", "blockers"):
            assert result[field] == core[field]
        assert result["findings"][:1] == core["findings"]
    else:
        incident = json.loads((tmp_path / result["incident"]).read_text())
        assert incident["reason"] == result["verdict"]
    if health_kind == "safe":
        assert not any("health telemetry warning" in item for item in result["findings"])
        assert json.loads(health.read_text())["status"] == result["status"]
    else:
        assert any("Nonblocking health telemetry warning:" in item for item in result["findings"])
    assert protected.read_text() == "unchanged\n"
    assert (tmp_path / "child-runs.txt").read_text() == "run\n"
    assert (tmp_path / "delivered.py").read_text() == "value = 2\n"


@pytest.mark.integration
@pytest.mark.parametrize("valid", [True, False])
def test_health_write_io_error_keeps_primary_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bool
) -> None:
    """Keep completed or failed delivery when the opened health stream fails to write."""
    original_fdopen = os.fdopen

    class FailingHealthStream:
        """Represent an opened OS stream whose storage rejects a write."""

        def __init__(self, descriptor: int, *args: object, **kwargs: object) -> None:
            self.stream = original_fdopen(descriptor, *args, **kwargs)

        def __enter__(self) -> FailingHealthStream:
            return self

        def __exit__(self, *args: object) -> None:
            self.stream.close()

        def write(self, text: str) -> None:
            raise OSError("sensitive filesystem diagnostic")

    monkeypatch.setattr(os, "fdopen", FailingHealthStream)
    core = _core()
    if not valid:
        del core["status"]

    result = _deliver(tmp_path, monkeypatch, core)

    assert result["status"] == ("complete" if valid else "blocked")
    assert result["verdict"] == (
        "Delivered." if valid else "invalid model result: model result must contain exactly the seven peer fields"
    )
    assert result["findings"][-1] == (
        "Nonblocking health telemetry warning: health record could not be written safely."
    )
    assert "sensitive filesystem diagnostic" not in json.dumps(result)
    assert (tmp_path / "child-runs.txt").read_text() == "run\n"


def _supports_fifo() -> bool:
    """Probe named-pipe creation before collection on hosts exposing that capability."""
    if not hasattr(os, "mkfifo"):
        return False
    with tempfile.TemporaryDirectory() as directory:
        try:
            os.mkfifo(Path(directory) / "health")
        except OSError:
            return False
        return True


@pytest.mark.integration
@pytest.mark.skipif(not _supports_fifo(), reason="requires named-pipe creation capability")
@pytest.mark.parametrize("replace_after_inspection", [False, True])
def test_fifo_health_does_not_stall_completed_delivery(tmp_path: Path, replace_after_inspection: bool) -> None:
    """Return completed child facts when a health FIFO has no reader, including an open race."""
    health = tmp_path / ".temp" / "bridge" / "health.jsonl"
    health.parent.mkdir(parents=True)
    if replace_after_inspection:
        health.write_text("original\n", encoding="utf-8")
    else:
        os.mkfifo(health)
    payload = json.dumps({"type": "result", "is_error": False, "structured_output": _core()})
    emitter = tmp_path / "peer.py"
    emitter.write_text(
        "from pathlib import Path\nPath('delivered.py').write_text('value = 2\\n')\n"
        "with Path('child-runs.txt').open('a') as stream: stream.write('run\\n')\n"
        f"print({payload!r})\n",
        encoding="utf-8",
    )
    source = (
        "import json, os, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(BIN_ROOT)!r})\nimport bridge_call\n"
        f"workspace = Path({str(tmp_path)!r})\nhealth = Path({str(health)!r})\n"
        f"bridge_call.build_claude_argv = lambda *_: [sys.executable, {str(emitter)!r}]\n"
    )
    if replace_after_inspection:
        source += (
            "original_open = os.open\n"
            "def replace_health(path, *args, **kwargs):\n"
            "    if Path(path) == health:\n"
            "        health.unlink()\n"
            "        os.mkfifo(health)\n"
            "    return original_open(path, *args, **kwargs)\n"
            "os.open = replace_health\n"
        )
    source += (
        "request = bridge_call.Request(verb='implement', task='Deliver.', model='fixture', effort='medium', "
        "timeout_seconds=1, depth=0, run_id='fifo', workspace=workspace, direction='codex_to_claude')\n"
        "print(json.dumps(bridge_call.run_request(request, host='claude')))\n"
    )

    completed = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=3, check=True)
    result = json.loads(completed.stdout)

    assert result["status"] == "complete"
    assert result["verdict"] == "Delivered."
    assert result["files_touched"] == ["delivered.py"]
    assert stat.S_ISFIFO(health.stat().st_mode)
    assert "Nonblocking health telemetry warning:" in result["findings"][-1]
    assert (tmp_path / "child-runs.txt").read_text() == "run\n"
    assert (tmp_path / "delivered.py").read_text() == "value = 2\n"
