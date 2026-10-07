"""Regression checks for pre-session manifest state restored around a pytest session."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from _bench_common import manifest_session


def _conftest_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    builder_result: Exception | None = None,
) -> tuple[SimpleNamespace, tuple[Path, ...]]:
    """Start a mocked manifest session over disposable paths, retaining the state needed for restoration.

    The caller must finish the session before undoing the supplied monkeypatch.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
    ...     session, outputs = _conftest_session(Path(directory), patch)
    ...     [path.read_text() for path in outputs]
    ...     manifest_session.finish_session(session)
    ...     outputs[0].read_text(), outputs[1].exists()
    ['regenerated', 'regenerated']
    ('pre-existing', False)
    """
    outputs = tuple(tmp_path / name for name in ("existing.json", "absent.json"))
    builders = (tmp_path / "builder.py",)

    def _fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        """Rewrite every declared output the way a real builder would, or fail."""
        if builder_result is not None:
            raise builder_result
        for path in outputs:
            path.write_text("regenerated", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(manifest_session, "_GENERATED_MANIFEST_PATHS", outputs)
    monkeypatch.setattr(manifest_session, "_MANIFEST_BUILDERS", builders)
    monkeypatch.setattr(subprocess, "run", _fake_run)
    outputs[0].write_text("pre-existing", encoding="utf-8")
    session = SimpleNamespace(config=SimpleNamespace())

    manifest_session.start_session(session)
    return session, outputs


def test_sessionfinish_restores_pre_existing_manifest_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest that existed before the session keeps its own bytes afterwards."""
    session, outputs = _conftest_session(tmp_path, monkeypatch)
    assert outputs[0].read_text(encoding="utf-8") == "regenerated"

    manifest_session.finish_session(session)

    assert outputs[0].read_text(encoding="utf-8") == "pre-existing"
    assert not outputs[1].exists()


def test_builder_failure_is_recorded_instead_of_aborting_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing manifest builder must not turn every unrelated test into a usage error."""
    failure = subprocess.CalledProcessError(1, ["builder"], output="", stderr="builder exploded")

    session, _ = _conftest_session(tmp_path, monkeypatch, builder_result=failure)

    artifacts = session.config._generated_manifest_artifacts
    assert artifacts.generation_count == 0
    assert artifacts.build_error == "builder exploded"
    assert manifest_session.finish_session(session) is None


def test_builder_failure_restores_pre_existing_manifest_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A build that fails after overwriting an output still leaves the checkout intact."""
    outputs = tuple(tmp_path / name for name in ("existing.json", "absent.json"))

    def _failing_builder(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        """Overwrite the pre-existing output, then fail like a partially-run chain."""
        outputs[0].write_text("half-written", encoding="utf-8")
        raise subprocess.CalledProcessError(1, command, output="", stderr="stopped mid-chain")

    monkeypatch.setattr(manifest_session, "_GENERATED_MANIFEST_PATHS", outputs)
    monkeypatch.setattr(manifest_session, "_MANIFEST_BUILDERS", (tmp_path / "builder.py",))
    monkeypatch.setattr(subprocess, "run", _failing_builder)
    outputs[0].write_text("pre-existing", encoding="utf-8")
    session = SimpleNamespace(config=SimpleNamespace())

    manifest_session.start_session(session)

    assert outputs[0].read_text(encoding="utf-8") == "pre-existing"
    assert not outputs[1].exists()
