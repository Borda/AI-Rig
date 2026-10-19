"""Executable acceptance contract for the installed Codex Rig package payload."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SAFE_TEST_MARKER = "installed_plugin"
INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS = 180


def test_installed_package_selection_timeout_is_bounded_for_native_windows() -> None:
    """Keep child execution bounded and leave the outer test time for setup and timeout cleanup."""
    assert 60 < INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS <= 300
    marks = getattr(test_installed_package_runs_the_explicit_package_safe_selection, "pytestmark", [])
    timeouts = [mark.args[0] for mark in marks if mark.name == "timeout"]
    assert len(timeouts) == 1, "the installed-package gate needs an explicit outer deadline"
    assert INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS + 60 <= timeouts[0] <= 300


def _package_payload_paths() -> tuple[str, ...]:
    """Return the exact shipped payload paths in manifest order.

    Example:
        >>> _package_payload_paths()[0]
        '.codex-mcp.json'
    """
    manifest = json.loads((PLUGIN_ROOT / "package-manifest.json").read_text(encoding="utf-8"))
    return tuple(record["path"] for record in manifest["files"])


def _copied_package_root(tmp_path: Path) -> Path:
    """Copy only the manifest-declared plugin payload into an isolated cache."""
    manifest = json.loads((PLUGIN_ROOT / "package-manifest.json").read_text(encoding="utf-8"))
    installed_root = tmp_path / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / manifest["version"]
    installed_root.mkdir(parents=True)
    for relative in _package_payload_paths():
        source = PLUGIN_ROOT / relative
        destination = installed_root / relative
        assert source.is_file(), f"manifested payload is absent: {relative}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    shutil.copy2(PLUGIN_ROOT / "package-manifest.json", installed_root / "package-manifest.json")
    return installed_root


@pytest.mark.packaging
@pytest.mark.integration
@pytest.mark.timeout(INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS + 60)
@pytest.mark.flaky(reruns=1, reruns_delay=1, condition=sys.platform == "win32", only_rerun="^TimeoutExpired:")
def test_installed_package_runs_the_explicit_package_safe_selection(tmp_path: Path) -> None:
    """Prevent checkout-only tests from being mistaken for installed-package coverage.

    The marker selects staged execution manifests, privacy-minimized telemetry, the complete parallel-write lifecycle,
    the denial protocol/client, all seven network approval briefs, both PR collector boundaries, and calibration
    scoring. The complete worktree module carries the marker because the shipped lifecycle contract requires it. A
    separate source-checkout suite retains the valid sync, CI-harness, and Git metadata contracts.

    Two child workers run the complete selection concurrently, bounding nested parallelism while reducing the Windows
    subprocess-heavy selection's runtime.

    The outer deadline includes package copying and child cleanup; sharing the child's deadline lets the Windows pytest-
    timeout thread terminate the worker before subprocess.run can reap and report a timed-out selection. One Windows
    subprocess-timeout retry mitigates unconfirmed runner timing sensitivity; assertion failures are final.

    Flushed phase timings retain completed work and active cases in the timeout failure, including teardown stalls.
    """
    installed_root = _copied_package_root(tmp_path)
    for path in (installed_root / "Makefile", installed_root / ".github", installed_root / ".git"):
        assert not path.exists(), f"installed payload must not include checkout context: {path.name}"
    assert not (tmp_path / ".git").exists()

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CODEX_RIG_PACKAGE_TIMINGS"] = "1"
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-u",
                "-m",
                "pytest",
                "-q",
                "-n",
                "2",
                "--strict-markers",
                "--durations=20",
                "-m",
                PACKAGE_SAFE_TEST_MARKER,
            ],
            cwd=installed_root,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        # TimeoutExpired retains bytes even with text=True; keep its type so the Windows retry still applies.
        for stream, output in (("stdout", error.stdout), ("stderr", error.stderr)):
            if output:
                text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
                print(f"Installed-package child {stream} before timeout:\n{text}", flush=True)
        raise

    assert result.returncode == 0, result.stdout + result.stderr
    for path in (installed_root / "Makefile", installed_root / ".github", installed_root / ".git"):
        assert not path.exists(), f"test execution created checkout context: {path.name}"


def test_installed_package_timeout_prints_partial_streams_and_preserves_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Display partial child diagnostics without changing the Windows retry's exception type."""
    error = subprocess.TimeoutExpired([sys.executable, "-m", "pytest"], 180, b"completed case\n\xff", b"child stderr\n")

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Supply a timeout only for this diagnostic transport check."""
        raise error

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        test_installed_package_runs_the_explicit_package_safe_selection(tmp_path)

    assert raised.value is error
    output = capsys.readouterr().out
    assert "completed case\n\ufffd" in output
    assert "child stderr" in output
