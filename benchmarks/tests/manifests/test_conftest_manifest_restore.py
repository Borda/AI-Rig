"""Regression checks for pre-session manifest state restored around a pytest session."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from _bench_common import manifest_session

Builder = Callable[[list[str], tuple[Path, ...]], subprocess.CompletedProcess[str]]


def _regenerate_outputs(command: list[str], outputs: tuple[Path, ...]) -> subprocess.CompletedProcess[str]:
    """Rewrite every declared output the way a real builder would."""
    for path in outputs:
        path.write_text("regenerated", encoding="utf-8")
    return subprocess.CompletedProcess(command, 0, "", "")


def _explode(command: list[str], outputs: tuple[Path, ...]) -> subprocess.CompletedProcess[str]:
    """Fail immediately without touching any declared output."""
    raise subprocess.CalledProcessError(1, command, output="", stderr="builder exploded")


def _overwrite_then_fail(command: list[str], outputs: tuple[Path, ...]) -> subprocess.CompletedProcess[str]:
    """Overwrite the pre-existing output, then fail like a partially-run chain."""
    outputs[0].write_text("half-written", encoding="utf-8")
    raise subprocess.CalledProcessError(1, command, output="", stderr="stopped mid-chain")


def _conftest_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, builder: Builder = _regenerate_outputs
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

    def _fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        """Route the builder subprocess call to the supplied fake."""
        return builder(command, outputs)

    monkeypatch.setattr(manifest_session, "_GENERATED_MANIFEST_PATHS", outputs)
    monkeypatch.setattr(manifest_session, "_MANIFEST_BUILDERS", (tmp_path / "builder.py",))
    monkeypatch.setattr(subprocess, "run", _fake_run)
    outputs[0].write_text("pre-existing", encoding="utf-8")
    session = SimpleNamespace(config=SimpleNamespace())

    manifest_session.start_session(session)
    return session, outputs


class TestManifestSession:
    """Manifest builders regenerate outputs for a session and leave the checkout as they found it."""

    def test_finish_restores_pre_existing_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A manifest that existed before the session keeps its own bytes afterwards.

        The builder overwrites the existing manifest and creates a missing one; finishing the session must put back the
        original bytes and delete the file that did not exist, so a test run never leaves regenerated artifacts behind.
        """
        session, outputs = _conftest_session(tmp_path, monkeypatch)
        assert outputs[0].read_text(encoding="utf-8") == "regenerated"  # restoring unchanged bytes would prove nothing

        manifest_session.finish_session(session)

        assert outputs[0].read_text(encoding="utf-8") == "pre-existing"
        assert not outputs[1].exists()

    def test_builder_failure_is_recorded_instead_of_aborting_collection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing manifest builder must not turn every unrelated test into a usage error.

        The failure is kept on the session artifacts so a test that needs the manifest can report it, while collection
        of everything else proceeds and finishing the session has nothing to undo.
        """
        session, _ = _conftest_session(tmp_path, monkeypatch, builder=_explode)

        artifacts = session.config._generated_manifest_artifacts
        assert artifacts.generation_count == 0
        assert artifacts.build_error == "builder exploded"
        assert manifest_session.finish_session(session) is None

    def test_builder_failure_restores_pre_existing_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A build that fails after overwriting an output still leaves the checkout intact.

        A chain that dies midway has already rewritten the first manifest; the session must roll that back on its own
        because no later finish step is guaranteed to run after a failed start.
        """
        _, outputs = _conftest_session(tmp_path, monkeypatch, builder=_overwrite_then_fail)

        assert outputs[0].read_text(encoding="utf-8") == "pre-existing"
        assert not outputs[1].exists()
