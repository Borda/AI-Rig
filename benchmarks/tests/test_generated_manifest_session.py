"""Regression checks for generated manifests required during test collection."""

from __future__ import annotations

import io
import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _bench_common import manifest_io, manifest_session

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BENCHMARKS_DIR.parent


@pytest.mark.integration
@pytest.mark.parametrize(
    "builder",
    [
        "build-provider-parity-methodology-manifest.py",
        "build-codex-integration-manifest.py",
        "build-codex-agentic-manifest.py",
    ],
)
def test_session_makes_manifests_ready_once_with_byte_stable_outputs(
    generated_manifest_artifacts: Any,
    builder: str,
) -> None:
    """Session generation must make every manifest available and satisfy read-only checks."""
    artifacts = generated_manifest_artifacts

    assert artifacts.generation_count == 1
    assert all(path.is_file() for path in artifacts.paths)

    result = subprocess.run(
        [sys.executable, str(BENCHMARKS_DIR / builder), "--check"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_session_hook_recreates_only_absent_outputs_and_cleans_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean checkout must build the chain once, then leave no generated outputs behind."""
    outputs = tuple(
        tmp_path / name
        for name in ("methodology.json", "integration.json", "integration.md", "agentic.json", "agentic.md")
    )
    builders = tuple(tmp_path / name for name in ("methodology.py", "integration.py", "agentic.py"))
    calls: list[tuple[str, ...]] = []

    def _fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        """Record the dependency chain and materialize its final generated files."""
        calls.append(tuple(command))
        if len(calls) == len(builders):
            for path in outputs:
                path.write_text(path.name, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(manifest_session, "_GENERATED_MANIFEST_PATHS", outputs)
    monkeypatch.setattr(manifest_session, "_MANIFEST_BUILDERS", builders)
    monkeypatch.setattr(subprocess, "run", _fake_run)
    session = SimpleNamespace(config=SimpleNamespace())

    manifest_session.start_session(session)

    artifacts = session.config._generated_manifest_artifacts
    assert artifacts.initially_missing == outputs
    assert artifacts.generation_count == 1
    assert calls == [(sys.executable, str(builder)) for builder in builders]
    assert all(path.is_file() for path in outputs)

    manifest_session.finish_session(session)

    assert not any(path.exists() for path in outputs)


@pytest.mark.parametrize(
    ("generation_count", "build_error"),
    [
        pytest.param(1, None, id="successful-build"),
        pytest.param(0, "builder failed", id="failed-build"),
    ],
)
def test_xdist_worker_uses_controller_manifest_result_without_rebuilding_or_restoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generation_count: int, build_error: str | None
) -> None:
    """A worker must leave the controller's shared manifest files untouched."""
    output = tmp_path / "methodology.json"
    output.write_text("controller output", encoding="utf-8")
    monkeypatch.setattr(manifest_session, "_GENERATED_MANIFEST_PATHS", (output,))

    def _unexpected_build(*_: object, **__: object) -> None:
        """Fail if a worker starts its own manifest build."""
        pytest.fail("worker rebuilt the controller manifest")

    monkeypatch.setattr(manifest_session, "_generate_manifest_artifacts", _unexpected_build)
    session = SimpleNamespace(
        config=SimpleNamespace(workerinput={"generated_manifest_result": (generation_count, build_error)})
    )

    manifest_session.start_session(session)
    artifacts = session.config._generated_manifest_artifacts
    assert artifacts.generation_count == generation_count
    assert artifacts.build_error == build_error

    manifest_session.finish_session(session)
    assert output.read_text(encoding="utf-8") == "controller output"


@pytest.mark.parametrize(
    "writer_name",
    [
        pytest.param("build-provider-parity-methodology-manifest.py", id="methodology"),
        pytest.param("build-codex-integration-manifest.py", id="integration"),
        pytest.param("build-codex-agentic-manifest.py", id="agentic"),
        pytest.param("restore", id="session-restore"),
    ],
)
def test_manifest_readers_never_observe_a_truncated_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer_name: str
) -> None:
    """Read at the write boundary that nested pytest sessions can overlap with launcher readers."""
    output = tmp_path / "manifest.json"
    previous = b'{"generation": "previous"}\n'
    replacement = b'{"generation": "replacement"}\n'
    output.write_bytes(previous)
    observed: list[bytes] = []
    real_open = io.open

    class ObservedWriter:
        """Keep real file I/O while exposing the moment immediately before the bytes are written."""

        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            self.stream.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self.stream, name)

        def write(self, payload: bytes) -> int:
            observed.append(output.read_bytes())
            return self.stream.write(payload)

    def observe_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = real_open(file, mode, *args, **kwargs)
        return ObservedWriter(stream) if mode == "wb" else stream

    monkeypatch.setattr(io, "open", observe_open)
    if writer_name == "restore":
        manifest_session._restore_manifests({output: replacement}, ())
    else:
        builder = runpy.run_path(str(BENCHMARKS_DIR / writer_name))
        builder["_write_or_check"](output, replacement, check=False)

    assert observed
    assert all(payload == previous for payload in observed)
    assert json.loads(output.read_bytes()) == {"generation": "replacement"}
    assert list(tmp_path.iterdir()) == [output]


def test_failed_manifest_publication_preserves_bytes_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed rename must neither truncate the old manifest nor leave a stray staging file."""
    output = tmp_path / "manifest.json"
    output.write_bytes(b'{"generation":"previous"}')

    def fail_replace(source: Path, destination: Path) -> None:
        assert source.parent == destination.parent == tmp_path
        assert source.read_bytes() == b'{"generation":"replacement"}'
        assert destination.read_bytes() == b'{"generation":"previous"}'
        raise PermissionError("publication denied")

    monkeypatch.setattr(manifest_io.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="publication denied"):
        manifest_io.write_manifest_bytes(output, b'{"generation":"replacement"}')
    assert output.read_bytes() == b'{"generation":"previous"}'
    assert list(tmp_path.iterdir()) == [output]


def test_identical_manifest_bytes_do_not_republish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nested collection sessions must avoid replacing already-current generated coordinates."""
    output = tmp_path / "manifest.json"
    payload = b'{"generation":"same"}'
    manifest_io.write_manifest_bytes(output, payload)

    def unexpected_replace(*args: Any) -> None:
        pytest.fail("unchanged manifest was republished")

    monkeypatch.setattr(manifest_io.os, "replace", unexpected_replace)
    manifest_io.write_manifest_bytes(output, payload)
    assert output.read_bytes() == payload
    assert list(tmp_path.iterdir()) == [output]
