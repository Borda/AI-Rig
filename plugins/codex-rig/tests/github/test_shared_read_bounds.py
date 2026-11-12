"""Exercise live subprocess output bounds without contacting GitHub."""

from __future__ import annotations

import errno
import io
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from _modules import load_module

SHARED = Path(__file__).resolve().parents[2] / "shared"


@pytest.fixture
def reader() -> ModuleType:
    """Load the standalone read transport."""
    return load_module("bounded_github_reader", SHARED / "github_read.py")


@pytest.mark.parametrize("flag", ["-cw", "-wc", "-cw=true"])
def test_browser_flag_clusters_never_execute(reader: ModuleType, flag: str) -> None:
    """Reject browser flags even when combined with another short option."""
    runner = mock.Mock(spec=subprocess.run, return_value=subprocess.CompletedProcess([], 0, b"{}", b""))
    with pytest.raises(reader.GitHubReadError, match="unsafe-gh-command:browser"):
        reader.run_gh_read(runner, ["gh", "pr", "view", "17", flag], timeout=5, label="browser")
    runner.assert_not_called()


@pytest.mark.parametrize("descriptor", [1, 2])
def test_default_transport_stops_writer_at_output_cap(
    reader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, descriptor: int
) -> None:
    """Kill an overflowing writer before it finishes producing its payload."""
    monkeypatch.setattr(reader, "MAX_OUTPUT_BYTES", 1024)
    marker = tmp_path / "completed"
    program = "import os,sys; os.write(int(sys.argv[1]), b'x'*(4*1024*1024)); open(sys.argv[2], 'w').write('finished')"
    with pytest.raises(reader.GitHubReadError, match="command-output-oversized"):
        reader._run_default_gh_command([sys.executable, "-c", program, str(descriptor), str(marker)], 10)
    assert not marker.exists()


def test_default_transport_accepts_exact_cap(reader: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep all bytes when both streams exactly meet the permitted limit."""
    monkeypatch.setattr(reader, "MAX_OUTPUT_BYTES", 1024)
    result = reader._run_default_gh_command(
        [sys.executable, "-c", "import os; os.write(1,b'x'*1024); os.write(2,b'y'*1024)"], 10
    )
    assert result.returncode == 0
    assert result.stdout == b"x" * 1024
    assert result.stderr == b"y" * 1024


def test_default_transport_preserves_timeout(reader: ModuleType) -> None:
    """Terminate a child that keeps its output pipes open past its deadline."""
    with pytest.raises(subprocess.TimeoutExpired):
        reader._run_default_gh_command([sys.executable, "-c", "import time; time.sleep(30)"], 0.1)


def test_default_transport_deadline_covers_inherited_pipes(reader: ModuleType) -> None:
    """Keep the deadline when an exited child leaves its output handles with a descendant."""
    program = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(1)'])"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        reader._run_default_gh_command([sys.executable, "-c", program], 0.1)
    assert time.monotonic() - started < 0.8


@pytest.fixture
def blocking_pipe_close(reader: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Event, Event]]:
    """Simulate Windows pipe cleanup and release every blocked worker during teardown."""
    release = Event()
    read_finished = Event()
    close_finished = Event()
    stream = mock.Mock(spec=io.RawIOBase)

    def read_output(_size: int) -> bytes:
        """Hold the reader as though a descendant still owns an output handle."""
        release.wait(2)
        read_finished.set()
        return b""

    def close_output() -> None:
        """Model a close waiting for the synchronous reader's descriptor lock."""
        release.wait(2)
        close_finished.set()

    stream.read.side_effect = read_output
    stream.close.side_effect = close_output
    process = mock.MagicMock(spec=subprocess.Popen)
    process.stdout = stream
    process.stderr = io.BytesIO()
    process.stdin = None
    process.__enter__.return_value = process
    real_exit = subprocess.Popen.__exit__
    process.__exit__.side_effect = lambda *args: real_exit(process, *args)
    monkeypatch.setattr(reader.subprocess, "Popen", mock.Mock(spec=subprocess.Popen, return_value=process))
    monkeypatch.setattr(reader, "sys", SimpleNamespace(platform="win32"))
    yield release, close_finished
    release.set()
    assert read_finished.wait(2)
    assert close_finished.wait(2)


def test_bounded_command_timeout_does_not_wait_for_pipe_close(
    reader: ModuleType, blocking_pipe_close: tuple[Event, Event]
) -> None:
    """Raise at the deadline while Windows-style pipe closing remains blocked.

    The subprocess boundary models an exited child whose descendant holds a pipe; cleanup must outlive the caller.
    """
    release, close_finished = blocking_pipe_close
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        reader.run_bounded_command(["child"], timeout=0.1, max_output_bytes=1024)
    assert time.monotonic() - started < 0.8
    assert not release.is_set()
    assert not close_finished.is_set()


@pytest.mark.parametrize("operation", ["write", "close"])
@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.EPIPE])
def test_bounded_command_preserves_child_failure_after_closed_stdin(
    reader: ModuleType, monkeypatch: pytest.MonkeyPatch, operation: str, error_number: int
) -> None:
    """Preserve a real child's status and diagnostics when its input handle has closed."""
    real_popen = subprocess.Popen
    payload = b"patch"

    def start_with_closed_stdin(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        """Inject only the platform-specific stdin failure around a real child."""
        process = real_popen(*args, **kwargs)
        process.stdin.close()
        input_stream = mock.MagicMock(spec=io.BytesIO)
        input_stream.__enter__.return_value = input_stream
        input_stream.__exit__.side_effect = lambda *_: input_stream.close()
        input_stream.write.return_value = len(payload)
        input_stream.close.return_value = None
        getattr(input_stream, operation).side_effect = [OSError(error_number, "consumer closed stdin"), None]
        process.stdin = input_stream
        return process

    monkeypatch.setattr(reader.subprocess, "Popen", start_with_closed_stdin)
    result = reader.run_bounded_command(
        [sys.executable, "-c", "import sys; sys.stderr.buffer.write(b'consumer rejected patch\\n'); sys.exit(7)"],
        timeout=10,
        max_output_bytes=1024,
        input_bytes=payload,
    )
    assert result.returncode == 7
    assert result.stdout == b""
    assert result.stderr == b"consumer rejected patch\n"


@pytest.mark.parametrize("operation", ["write", "close"])
def test_command_input_propagates_other_io_errors(reader: ModuleType, operation: str) -> None:
    """Keep unexpected write and close failures visible to the command owner."""
    stream = mock.MagicMock(spec=io.BytesIO)
    stream.__enter__.return_value = stream
    stream.__exit__.side_effect = lambda *_: stream.close()
    stream.close.return_value = None
    stream.write.return_value = 5
    getattr(stream, operation).side_effect = OSError(errno.EIO, "disk error")
    with pytest.raises(OSError, match="disk error") as error:
        reader._send_command_input(stream, b"patch", Event())
    assert error.value.errno == errno.EIO


def test_command_input_preserves_write_error_when_close_would_report_invalid_handle(reader: ModuleType) -> None:
    """Prevent a tolerated close error from masking an unrelated write failure."""
    stream = mock.MagicMock(spec=io.BytesIO)
    stream.write.side_effect = OSError(errno.EIO, "write failed")
    stream.close.side_effect = OSError(errno.EINVAL, "consumer closed stdin")
    with pytest.raises(OSError, match="write failed") as error:
        reader._send_command_input(stream, b"patch", Event())
    assert error.value.errno == errno.EIO


@pytest.mark.parametrize("descriptor", [1, 2])
def test_collector_stops_writer_at_output_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, descriptor: int) -> None:
    """Apply the same live bounds to the collector's local command path."""
    collector = load_module("bounded_pr_collector", SHARED / "collect_pr.py")
    monkeypatch.setattr(collector, "MAX_OUTPUT_BYTES", 1024)
    marker = tmp_path / "completed"
    program = "import os,sys; os.write(int(sys.argv[1]), b'x'*(4*1024*1024)); open(sys.argv[2], 'w').write('finished')"
    with pytest.raises(collector.CollectionError, match="command-output-oversized:local"):
        collector._run(subprocess.run, [sys.executable, "-c", program, str(descriptor), str(marker)], 10, "local")
    assert not marker.exists()


def test_collector_drains_output_while_sending_input() -> None:
    """Send a patch larger than pipe capacity while its consumer emits output."""
    collector = load_module("duplex_pr_collector", SHARED / "collect_pr.py")
    payload = b"p" * (256 * 1024)
    program = (
        "import sys; sys.stdout.buffer.write(b'x'*262144); sys.stdout.flush(); "
        "sys.stdout.buffer.write(str(len(sys.stdin.buffer.read())).encode('ascii') + b'\\n')"
    )
    result = collector._run(subprocess.run, [sys.executable, "-c", program], 10, "duplex", input_bytes=payload)
    assert result == b"x" * 262144 + b"262144\n"
