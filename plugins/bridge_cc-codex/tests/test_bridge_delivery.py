"""Real child-process regressions for result delivery and optional telemetry."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from _bridge_link_probes import file_symlinks_supported, hardlinks_supported

BIN_ROOT = Path(__file__).resolve().parents[1] / "bin"
if str(BIN_ROOT) not in sys.path:
    sys.path.insert(0, str(BIN_ROOT))

import bridge_call  # noqa: E402

INVALID_VERDICT = "invalid model result: model result must contain exactly the seven peer fields"
HEALTH_WARNING = "Nonblocking health telemetry warning:"
PEER_FACTS = ("files_touched", "remaining", "blockers")
_REAL_FDOPEN = os.fdopen
# Child-side patch: swap the inspected health file for a FIFO the moment the delivery code opens it.
_REPLACE_HEALTH_ON_OPEN = (
    "original_open = os.open\n"
    "def replace_health(path, *args, **kwargs):\n"
    "    if Path(path) == health:\n"
    "        health.unlink()\n"
    "        os.mkfifo(health)\n"
    "    return original_open(path, *args, **kwargs)\n"
    "os.open = replace_health\n"
)


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


_skip_no_hardlink = pytest.mark.skipif(not hardlinks_supported(), reason="requires hard-link creation capability")
_skip_no_symlink = pytest.mark.skipif(not file_symlinks_supported(), reason="requires file symlink creation capability")
_skip_no_fifo = pytest.mark.skipif(not _supports_fifo(), reason="requires named-pipe creation capability")


def _make_hardlink(health: Path, protected: Path) -> None:
    """Make the health path a second name for the protected file."""
    os.link(protected, health)


def _make_symlink(health: Path, protected: Path) -> None:
    """Make the health path a symlink to the protected file."""
    health.symlink_to(protected)


def _make_directory(health: Path, protected: Path) -> None:
    """Make the health path a directory, which cannot be opened as a log."""
    health.mkdir()


def _prepare_fifo(health: Path) -> None:
    """Start with a FIFO that has no reader."""
    os.mkfifo(health)


def _prepare_regular_file(health: Path) -> None:
    """Start with a regular file that passes inspection and is swapped for a FIFO only at open time."""
    health.write_text("original\n", encoding="utf-8")


UNSAFE_HEALTH_TARGETS = [
    pytest.param(_make_hardlink, id="hardlink", marks=_skip_no_hardlink),
    pytest.param(_make_symlink, id="symlink", marks=_skip_no_symlink),
    pytest.param(_make_directory, id="directory"),
]


def _core(without: tuple[str, ...] = (), **overrides: object) -> dict[str, object]:
    """Return valid result facts shared by delivery scenarios, with fields overridden or removed."""
    base = {
        "status": "complete",
        "verdict": "Delivered.",
        "findings": ["Verified local change."],
        "files_touched": ["delivered.py"],
        "remaining": [],
        "blockers": [],
        "details": [],
    }
    return {key: value for key, value in {**base, **overrides}.items() if key not in without}


def _pick(mapping: dict[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    """Select the named fields so a failure shows the differing facts side by side."""
    return {field: mapping[field] for field in fields}


def _write_peer(workspace: Path, core: dict[str, object]) -> Path:
    """Write a deterministic local peer that edits the listed files, records one run, and prints the result."""
    payload = json.dumps({"type": "result", "is_error": False, "structured_output": core})
    lines = ["from pathlib import Path"]
    lines += [f"Path({filename!r}).write_text('value = 2\\n')" for filename in core["files_touched"]]
    lines += ["with Path('child-runs.txt').open('a') as stream: stream.write('run\\n')", f"print({payload!r})"]
    emitter = workspace / "peer.py"
    emitter.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return emitter


def _assert_child_delivered_once(workspace: Path) -> None:
    """Check the peer ran exactly once and its edit landed, so a failed side channel never replays the child."""
    assert (workspace / "child-runs.txt").read_text() == "run\n"
    assert (workspace / "delivered.py").read_text() == "value = 2\n"


class FailingHealthStream:
    """Represent an opened OS stream whose storage rejects a write."""

    def __init__(self, descriptor: int, *args: object, **kwargs: object) -> None:
        """Wrap the real stream so closing still releases the descriptor."""
        self.stream = _REAL_FDOPEN(descriptor, *args, **kwargs)

    def __enter__(self) -> FailingHealthStream:
        """Enter the context without touching storage."""
        return self

    def __exit__(self, *args: object) -> None:
        """Close the wrapped stream."""
        self.stream.close()

    def write(self, text: str) -> None:
        """Reject every write with a diagnostic that must never reach the caller."""
        raise OSError("sensitive filesystem diagnostic")


@pytest.fixture
def deliver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, object]], dict[str, object]]:
    """Return a runner delivering one core result through the public foreground consumer and a local peer."""

    def run(core: dict[str, object]) -> dict[str, object]:
        """Run the local peer for ``core`` and return the delivered envelope."""
        emitter = _write_peer(tmp_path, core)
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

    return run


@pytest.fixture
def health_path(tmp_path: Path) -> Path:
    """Return the health log location, with its store directory created and the file itself absent."""
    store = tmp_path / ".temp" / "bridge"
    store.mkdir(parents=True)
    return store / "health.jsonl"


@pytest.fixture
def protected(tmp_path: Path) -> Path:
    """Create a file that an unsafe health target points at and that must never be modified."""
    path = tmp_path / "protected.txt"
    path.write_text("unchanged\n", encoding="utf-8")
    return path


class TestResultFacts:
    """Delivery keeps every fact the peer reports."""

    @pytest.mark.integration
    def test_real_child_preserves_all_result_facts(
        self, tmp_path: Path, deliver: Callable[[dict[str, object]], dict[str, object]]
    ) -> None:
        """Keep nine real edits and verbose peer evidence below actual capture limits.

        A real child edits nine files and reports oversized verdict, findings, remaining and blockers plus 33 long
        details; core fields must round-trip, details must land in the transcript, and the envelope must validate.
        """
        long_items = [f"{index}:" + "e" * 501 for index in range(9)]
        core = _core(
            files_touched=[f"changed_{index}.py" for index in range(9)],
            verdict="Delivered " + "v" * 501,
            findings=long_items,
            remaining=long_items,
            blockers=long_items,
            details=[f"{index}:" + "d" * 2001 for index in range(33)],
        )

        result = deliver(core)

        transcript = (tmp_path / result["transcript_path"]).read_text(encoding="utf-8")
        assert _pick(result, bridge_call.CORE_FIELDS) == _pick(core, bridge_call.CORE_FIELDS)
        assert all((tmp_path / filename).read_text() == "value = 2\n" for filename in core["files_touched"])
        assert all(detail in transcript for detail in core["details"])
        assert "details" not in result
        assert (tmp_path / "child-runs.txt").read_text() == "run\n"
        assert bridge_call.validate_envelope(result) == result


class TestHealthTelemetry:
    """Optional health telemetry never costs the caller the primary result or replays the child."""

    @pytest.mark.integration
    def test_safe_target_records_complete_result(
        self,
        tmp_path: Path,
        deliver: Callable[[dict[str, object]], dict[str, object]],
        health_path: Path,
        protected: Path,
    ) -> None:
        """A normal health log gets the completed result and adds no warning.

        The store exists but the log does not, so the record is created; the delivered facts match what the peer sent.
        """
        core = _core()

        result = deliver(core)

        assert result["status"] == "complete"
        assert result["verdict"] == core["verdict"]
        assert _pick(result, PEER_FACTS) == _pick(core, PEER_FACTS)
        assert result["findings"][:1] == core["findings"]
        assert not any("health telemetry warning" in item for item in result["findings"])
        assert json.loads(health_path.read_text())["status"] == "complete"
        assert protected.read_text() == "unchanged\n"
        _assert_child_delivered_once(tmp_path)

    @pytest.mark.integration
    def test_safe_target_records_blocked_result(
        self,
        tmp_path: Path,
        deliver: Callable[[dict[str, object]], dict[str, object]],
        health_path: Path,
        protected: Path,
    ) -> None:
        """A malformed peer result is blocked with an incident, and the blocked status is still logged.

        The peer omits ``status``; delivery must refuse it, write an incident naming the reason, and add no warning.
        """
        result = deliver(_core(without=("status",)))

        incident = json.loads((tmp_path / result["incident"]).read_text())
        assert result["status"] == "blocked"
        assert result["verdict"] == INVALID_VERDICT
        assert incident["reason"] == result["verdict"]
        assert not any("health telemetry warning" in item for item in result["findings"])
        assert json.loads(health_path.read_text())["status"] == "blocked"
        assert protected.read_text() == "unchanged\n"
        _assert_child_delivered_once(tmp_path)

    @pytest.mark.integration
    @pytest.mark.parametrize("make_target", UNSAFE_HEALTH_TARGETS)
    def test_unsafe_target_keeps_complete_result(
        self,
        tmp_path: Path,
        deliver: Callable[[dict[str, object]], dict[str, object]],
        health_path: Path,
        protected: Path,
        make_target: Callable[[Path, Path], None],
    ) -> None:
        """Keep a completed result and refuse unsafe telemetry without replaying the child.

        The health path is a hard link, symlink or directory; the primary result must survive with a warning, and the
        protected file the link points at must stay untouched.
        """
        make_target(health_path, protected)
        core = _core()

        result = deliver(core)

        assert result["status"] == "complete"
        assert result["verdict"] == core["verdict"]
        assert _pick(result, PEER_FACTS) == _pick(core, PEER_FACTS)
        assert result["findings"][:1] == core["findings"]
        assert any(HEALTH_WARNING in item for item in result["findings"])
        assert protected.read_text() == "unchanged\n"
        _assert_child_delivered_once(tmp_path)

    @pytest.mark.integration
    @pytest.mark.parametrize("make_target", UNSAFE_HEALTH_TARGETS)
    def test_unsafe_target_keeps_blocked_result(
        self,
        tmp_path: Path,
        deliver: Callable[[dict[str, object]], dict[str, object]],
        health_path: Path,
        protected: Path,
        make_target: Callable[[Path, Path], None],
    ) -> None:
        """Keep a blocked result and its incident when the health target is also unsafe.

        Both the malformed peer result and the unsafe log occur together; neither failure may hide the other, and the
        protected file must stay untouched.
        """
        make_target(health_path, protected)

        result = deliver(_core(without=("status",)))

        incident = json.loads((tmp_path / result["incident"]).read_text())
        assert result["status"] == "blocked"
        assert result["verdict"] == INVALID_VERDICT
        assert incident["reason"] == result["verdict"]
        assert any(HEALTH_WARNING in item for item in result["findings"])
        assert protected.read_text() == "unchanged\n"
        _assert_child_delivered_once(tmp_path)

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("without", "status", "verdict"),
        [
            pytest.param((), "complete", "Delivered.", id="valid"),
            pytest.param(("status",), "blocked", INVALID_VERDICT, id="invalid"),
        ],
    )
    def test_write_io_error_keeps_primary_result(
        self,
        tmp_path: Path,
        deliver: Callable[[dict[str, object]], dict[str, object]],
        monkeypatch: pytest.MonkeyPatch,
        without: tuple[str, ...],
        status: str,
        verdict: str,
    ) -> None:
        """Keep completed or failed delivery when the opened health stream fails to write.

        The storage layer rejects the health write with a sensitive diagnostic; the caller gets the primary result plus
        a generic warning that does not leak the diagnostic, and the child runs once.
        """
        monkeypatch.setattr(os, "fdopen", FailingHealthStream)

        result = deliver(_core(without=without))

        assert result["status"] == status
        assert result["verdict"] == verdict
        assert result["findings"][-1] == f"{HEALTH_WARNING} health record could not be written safely."
        assert "sensitive filesystem diagnostic" not in json.dumps(result)
        assert (tmp_path / "child-runs.txt").read_text() == "run\n"

    @pytest.mark.integration
    @_skip_no_fifo
    @pytest.mark.parametrize(
        ("prepare", "race_patch"),
        [
            pytest.param(_prepare_fifo, "", id="fifo-without-reader"),
            pytest.param(_prepare_regular_file, _REPLACE_HEALTH_ON_OPEN, id="replaced-after-inspection"),
        ],
    )
    def test_fifo_target_does_not_stall_completed_delivery(
        self, tmp_path: Path, health_path: Path, prepare: Callable[[Path], None], race_patch: str
    ) -> None:
        """Return completed child facts when a health FIFO has no reader, including an open race.

        A real child process runs the delivery under a hard timeout: opening a reader-less FIFO would block forever, so
        finishing proves the log is never opened in a blocking way, even if it is swapped for a FIFO after inspection.
        """
        prepare(health_path)
        emitter = _write_peer(tmp_path, _core())
        source = (
            "import json, os, sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(BIN_ROOT)!r})\nimport bridge_call\n"
            f"workspace = Path({str(tmp_path)!r})\nhealth = Path({str(health_path)!r})\n"
            f"bridge_call.build_claude_argv = lambda *_: [sys.executable, {str(emitter)!r}]\n"
            f"{race_patch}"
            "request = bridge_call.Request(verb='implement', task='Deliver.', model='fixture', effort='medium', "
            "timeout_seconds=1, depth=0, run_id='fifo', workspace=workspace, direction='codex_to_claude')\n"
            "print(json.dumps(bridge_call.run_request(request, host='claude')))\n"
        )

        completed = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True, timeout=3, check=True
        )

        result = json.loads(completed.stdout)
        assert result["status"] == "complete"
        assert result["verdict"] == "Delivered."
        assert result["files_touched"] == ["delivered.py"]
        assert stat.S_ISFIFO(health_path.stat().st_mode)
        assert HEALTH_WARNING in result["findings"][-1]
        _assert_child_delivered_once(tmp_path)
