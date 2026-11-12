"""Real child-process regressions for result delivery and optional telemetry."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from _bridge_link_probes import directory_symlinks_supported, file_symlinks_supported, hardlinks_supported

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
    "    if Path(path) == health or (Path(path) == Path(health.name) and kwargs.get('dir_fd') is not None):\n"
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


def _supports_private_mode() -> bool:
    """Probe whether the filesystem enforces owner-only file permission bits."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "private"
        path.write_text("probe", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            return False
        return stat.S_IMODE(path.stat().st_mode) == 0o600


_skip_no_hardlink = pytest.mark.skipif(not hardlinks_supported(), reason="requires hard-link creation capability")
_skip_no_symlink = pytest.mark.skipif(not file_symlinks_supported(), reason="requires file symlink creation capability")
_skip_no_directory_symlink = pytest.mark.skipif(
    not directory_symlinks_supported(), reason="requires directory symlink creation capability"
)
_skip_no_fifo = pytest.mark.skipif(not _supports_fifo(), reason="requires named-pipe creation capability")
_skip_no_private_mode = pytest.mark.skipif(not _supports_private_mode(), reason="requires private file-mode capability")


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


def _accounting_output(host: str, *, failed: bool = False) -> str:
    """Render actual provider event shapes with observable usage, including failed attempts."""
    error = {"code": "unsupported_value", "param": "reasoning.effort", "message": "unsupported effort"}
    if host == "claude":
        record = {"usage": {"input_tokens": 17, "output_tokens": 4}, "total_cost_usd": 0.125}
        record.update(
            {"is_error": True, "result": "unsupported_value: reasoning.effort: unsupported effort", "error": error}
            if failed
            else {"structured_output": _core()}
        )
        return json.dumps(record)
    records = [{"type": "turn.completed", "usage": {"input_tokens": 17, "output_tokens": 4, "cost": 0.125}}]
    records.append(
        {"type": "turn.failed", "error": error}
        if failed
        else {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(_core())}}
    )
    return "\n".join(json.dumps(record) for record in records)


def _install_accounting_peer(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    *,
    first_fault: str | None = None,
    fail_retry: bool = False,
) -> bridge_call.Request:
    """Replace only provider argv with a real local process that emits distinct attempt evidence."""
    emitter = workspace / "accounting-peer.py"
    emitter.write_text(
        "import sys, time\nfrom pathlib import Path\n"
        "runs = Path('accounting-runs.txt')\n"
        # Exercise a retry scheduled out beyond a subsecond cutoff before it can record its start.
        "if runs.exists(): time.sleep(0.8)\n"
        "first = not runs.exists()\n"
        "with runs.open('a') as stream: stream.write('run\\n')\n"
        f"fault = {first_fault!r}\n"
        f"failed = (first and fault == 'unsupported-effort') or (not first and {fail_retry!r})\n"
        f"output = {_accounting_output(host, failed=True)!r} if failed else {_accounting_output(host)!r}\n"
        "print(output, flush=True)\n"
        "time.sleep(10 if first and fault == 'timeout' else 0.06)\n"
        "sys.exit(1 if failed else 0)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge_call, f"build_{host}_argv", lambda *_: [sys.executable, str(emitter)])
    return bridge_call.Request(
        verb="review",
        task="Inspect local changes.",
        model="fixture",
        effort="medium",
        # Accounting assertions need startup headroom; the ten-second peer stall still exercises a real timeout.
        timeout_seconds=5,
        depth=0,
        run_id="accounting",
        workspace=workspace,
        direction="codex_to_claude" if host == "claude" else "claude_to_codex",
    )


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


def _reject_health_append(descriptor: int, mode: str, *args: object, **kwargs: object) -> object:
    """Reject append writes while preserving real reads used by independent retention maintenance."""
    if mode == "a":
        return FailingHealthStream(descriptor, mode, *args, **kwargs)
    return _REAL_FDOPEN(descriptor, mode, *args, **kwargs)


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
        monkeypatch.setattr(os, "fdopen", _reject_health_append)

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


class TestFinalResultAccounting:
    """Final accounting links public delivery to complete private evidence across retries."""

    @pytest.mark.integration
    @pytest.mark.parametrize("host", ["codex", "claude"])
    def test_final_health_links_lossless_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
    ) -> None:
        """Record one final summary and a lossless private report for either bridge direction."""
        request = _install_accounting_peer(tmp_path, monkeypatch, host)

        result = bridge_call.run_request(request, host=host)

        records = (tmp_path / ".temp" / "bridge" / "health.jsonl").read_text().splitlines()
        assert len(records) == 1
        health = json.loads(records[0])
        assert health["schema_version"] == 1
        report_path = tmp_path / health["report_path"]
        report = json.loads(report_path.read_text())
        assert report == {
            "schema_version": 1,
            "elapsed_seconds": health["elapsed_seconds"],
            "started_at": health["started_at"],
            "task_bytes": health["task_bytes"],
            "response_bytes": health["response_bytes"],
            "telemetry": health["telemetry"],
            **result,
        }
        assert health["started_at"] <= health["ts"]
        assert health["task_bytes"] == len(request.task.encode("utf-8"))
        assert health["response_bytes"] > 0
        attempt = health["telemetry"]["attempts"][0]
        assert health["started_at"] <= attempt["started_at"] <= attempt["ended_at"] <= health["ts"]
        assert attempt["returncode"] == 0
        assert attempt["timed_out"] is False
        assert attempt["stdout_bytes"] > 0
        assert report_path.name.startswith("result-")
        assert health["direction"] == request.direction
        assert health["transcript_path"] == result["transcript_path"]
        assert health["incident"] is None
        assert health["findings_count"] == 1
        assert health["blockers_count"] == 0
        assert health["remaining_count"] == 0
        assert health["elapsed_seconds"] >= result["duration_seconds"] > 0
        assert result["cost"] == 0.125
        assert result["tokens"] == {"input_tokens": 17.0, "output_tokens": 4.0}
        assert bridge_call.validate_envelope(result) == result

    @pytest.mark.integration
    @pytest.mark.parametrize("host", ["codex", "claude"])
    @pytest.mark.parametrize("first_fault", ["timeout", "unsupported-effort"])
    @pytest.mark.parametrize("fail_retry", [False, True])
    def test_retry_records_one_final_summary_and_observed_failed_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, first_fault: str, fail_retry: bool
    ) -> None:
        """Keep failed-attempt metrics and chain incidents while final accounting covers both real processes."""
        request = _install_accounting_peer(tmp_path, monkeypatch, host, first_fault=first_fault, fail_retry=fail_retry)
        started = time.monotonic()

        result = bridge_call.run_request(request, host=host)

        elapsed = time.monotonic() - started
        records = (tmp_path / ".temp" / "bridge" / "health.jsonl").read_text().splitlines()
        assert len(records) == 1
        health = json.loads(records[0])
        report = json.loads((tmp_path / health["report_path"]).read_text())
        incidents = [
            json.loads(path.read_text()) for path in (tmp_path / ".temp" / "bridge" / "incidents").glob("*.json")
        ]
        first = next(incident for incident in incidents if incident["prior_incident"] is None)
        assert (tmp_path / "accounting-runs.txt").read_text() == "run\nrun\n"
        assert result["status"] == ("blocked" if fail_retry else "complete")
        assert len(incidents) == (2 if fail_retry else 1)
        assert first["schema_version"] == 1
        assert first["run_id"] == request.run_id
        assert first["direction"] == request.direction
        assert first["fault"] == first_fault
        assert first["duration_seconds"] > 0
        assert first["tokens"] == {"input_tokens": 17.0, "output_tokens": 4.0}
        assert first["cost"] == 0.125
        assert (tmp_path / first["transcript_path"]).is_file()
        assert health["incident"] == result["incident"]
        assert health["elapsed_seconds"] >= first["duration_seconds"] + result["duration_seconds"]
        assert health["elapsed_seconds"] <= elapsed
        assert health["findings_count"] == len(result["findings"])
        assert health["blockers_count"] == len(result["blockers"])
        assert report == {
            "schema_version": 1,
            "elapsed_seconds": health["elapsed_seconds"],
            "started_at": health["started_at"],
            "task_bytes": health["task_bytes"],
            "response_bytes": health["response_bytes"],
            "telemetry": health["telemetry"],
            **result,
        }

    @pytest.mark.integration
    def test_post_run_protected_warning_is_in_report_and_summary(
        self, tmp_path: Path, deliver: Callable[[dict[str, object]], dict[str, object]]
    ) -> None:
        """Persist the protected-path warning added after the peer finishes, with final finding counts."""
        (tmp_path / ".codex").mkdir()

        result = deliver(_core(files_touched=[".codex/settings.json", "delivered.py"]))

        health = json.loads((tmp_path / ".temp" / "bridge" / "health.jsonl").read_text())
        report = json.loads((tmp_path / health["report_path"]).read_text())
        assert "Protected workspace path changed" in result["findings"][-1]
        assert report["findings"] == result["findings"]
        assert health["findings_count"] == 2

    @pytest.mark.integration
    def test_report_write_failure_preserves_delivery_and_health(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deliver: Callable[[dict[str, object]], dict[str, object]]
    ) -> None:
        """A rejected report rename adds a fixed warning without leaking storage diagnostics or replaying work."""
        original_replace = Path.replace

        def reject_report(source: Path, target: object) -> Path:
            """Fail only the report's atomic filesystem publication, leaving other artifact I/O real."""
            if Path(str(target)).name.startswith("result-"):
                raise OSError("sensitive report storage diagnostic")
            return original_replace(source, target)

        monkeypatch.setattr(Path, "replace", reject_report)

        result = deliver(_core())

        health = json.loads((tmp_path / ".temp" / "bridge" / "health.jsonl").read_text())
        assert result["status"] == "complete"
        assert result["findings"][-1] == "Nonblocking result report warning: result report could not be written safely."
        assert "sensitive report storage diagnostic" not in json.dumps(result)
        assert health["report_path"] is None
        assert health["findings_count"] == 2
        _assert_child_delivered_once(tmp_path)

    @pytest.mark.integration
    def test_health_failure_warning_is_persisted_in_result_report(
        self,
        tmp_path: Path,
        deliver: Callable[[dict[str, object]], dict[str, object]],
        health_path: Path,
    ) -> None:
        """Keep the returned health warning in the lossless report even though health publication happens last."""
        health_path.mkdir()

        result = deliver(_core())

        reports = list((tmp_path / ".temp" / "bridge").glob("result-*.json"))
        assert len(reports) == 1
        report = json.loads(reports[0].read_text())
        assert report["findings"] == result["findings"]
        assert result["findings"][-1] == f"{HEALTH_WARNING} health record could not be written safely."
        _assert_child_delivered_once(tmp_path)

    @pytest.mark.integration
    @_skip_no_private_mode
    def test_result_report_is_private_even_with_permissive_umask(
        self, tmp_path: Path, deliver: Callable[[dict[str, object]], dict[str, object]]
    ) -> None:
        """Create sensitive result details with owner-only permissions independently of the caller's umask."""
        previous_umask = os.umask(0)
        try:
            result = deliver(_core())
        finally:
            os.umask(previous_umask)

        health = json.loads((tmp_path / ".temp" / "bridge" / "health.jsonl").read_text())
        report_path = tmp_path / health["report_path"]
        assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
        assert json.loads(report_path.read_text())["verdict"] == result["verdict"]

    @pytest.mark.integration
    def test_unstarted_incident_uses_null_observed_metrics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused nested call records unavailable attempt observations as null rather than invented values."""
        request = _install_accounting_peer(tmp_path, monkeypatch, "codex")
        nested = bridge_call.Request(**{**request.__dict__, "depth": 1})

        result = bridge_call.run_request(nested, host="codex")

        incident = json.loads((tmp_path / result["incident"]).read_text())
        assert result["status"] == "refused"
        assert incident["schema_version"] == 1
        assert incident["run_id"] == nested.run_id
        assert incident["direction"] == nested.direction
        assert incident["duration_seconds"] is None
        assert incident["tokens"] is None
        assert incident["cost"] is None
        assert not (tmp_path / "accounting-runs.txt").exists()

    @pytest.mark.integration
    @_skip_no_directory_symlink
    @pytest.mark.parametrize("replacement_phase", ["before-constructor", "after-constructor"])
    def test_replaced_artifact_ancestry_keeps_completed_delivery(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        deliver: Callable[[dict[str, object]], dict[str, object]],
        replacement_phase: str,
    ) -> None:
        """Optional accounting cannot erase completed work when artifact ancestry escapes after child delivery."""
        external = tmp_path.parent / f"{tmp_path.name}-external-artifacts"
        external.mkdir()
        original_changes = bridge_call._protected_changes
        original_initialize = bridge_call.BridgePaths.__post_init__

        def replace_ancestry() -> None:
            """Replace the completed artifact directory while preserving its existing transcript evidence."""
            artifacts = tmp_path / ".temp" / "bridge"
            artifacts.rename(artifacts.with_name("retained-bridge"))
            artifacts.symlink_to(external, target_is_directory=True)

        def initialize_then_replace(paths: bridge_call.BridgePaths) -> None:
            """Inject the real filesystem race between constructor containment and the root property check."""
            original_initialize(paths)
            replace_ancestry()

        def changes_then_replace(before: dict[str, str], workspace: Path) -> list[str]:
            """Delay the ancestry attack until the actual child and protected-path inspection have completed."""
            changed = original_changes(before, workspace)
            if replacement_phase == "before-constructor":
                replace_ancestry()
            else:
                monkeypatch.setattr(bridge_call.BridgePaths, "__post_init__", initialize_then_replace)
            return changed

        monkeypatch.setattr(bridge_call, "_protected_changes", changes_then_replace)

        result = deliver(_core())

        assert result["status"] == "complete"
        assert result["verdict"] == "Delivered."
        assert "Nonblocking result report warning: result report could not be written safely." in result["findings"]
        assert list(external.iterdir()) == []
        retained_transcript = tmp_path / ".temp" / "retained-bridge" / Path(result["transcript_path"]).name
        assert "Delivered." in retained_transcript.read_text()
        _assert_child_delivered_once(tmp_path)
