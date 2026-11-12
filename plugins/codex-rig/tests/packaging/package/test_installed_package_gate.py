"""Executable acceptance contract for the installed Codex Rig package payload."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_SAFE_TEST_MARKER = "installed_plugin"
#: Keep the measured Windows acceptance bound while sharing expensive fixture variants across shards and workers.
INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS = 240
INSTALLED_PACKAGE_SHARDS = 8
#: Checkout-only entries a copied package payload must neither ship nor grow while its tests run.
CHECKOUT_CONTEXT_NAMES = ("Makefile", ".github", ".git")


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


def _checkout_context_present(installed_root: Path) -> list[str]:
    """Return the checkout-only entries that exist inside the copied package root."""
    return [name for name in CHECKOUT_CONTEXT_NAMES if (installed_root / name).exists()]


def _run_package_safe_selection(installed_root: Path, shard: int) -> subprocess.CompletedProcess[str]:
    """Run one shard of the package-safe selection in a child pytest, printing its partial streams on timeout.

    A timeout re-raises the original ``TimeoutExpired`` after printing whatever the child flushed, so the failure still
    names the completed and active cases.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CODEX_RIG_PACKAGE_TIMINGS"] = "1"
    try:
        return subprocess.run(
            [
                sys.executable,
                "-u",
                "-m",
                "pytest",
                "-q",
                "-n",
                "2",
                "--dist",
                "loadgroup",
                "--strict-markers",
                "--durations=20",
                "-m",
                PACKAGE_SAFE_TEST_MARKER,
                "--installed-package-shard",
                f"{shard}/{INSTALLED_PACKAGE_SHARDS}",
            ],
            cwd=installed_root,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        # TimeoutExpired retains bytes even with text=True; preserve the exception and its diagnostics.
        for stream, output in (("stdout", error.stdout), ("stderr", error.stderr)):
            if output:
                text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
                print(f"Installed-package child {stream} before timeout:\n{text}", flush=True)
        raise


@pytest.mark.packaging
@pytest.mark.integration
@pytest.mark.timeout(INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS + 60)
@pytest.mark.parametrize("shard", range(INSTALLED_PACKAGE_SHARDS))
def test_installed_package_runs_the_explicit_package_safe_selection(tmp_path: Path, shard: int) -> None:
    """Prevent checkout-only tests from being mistaken for installed-package coverage.

    The marker selects staged execution manifests, privacy-minimized telemetry, the complete parallel-write lifecycle,
    the denial protocol/client, all seven network approval briefs, both PR collector boundaries, and calibration
    scoring. The complete worktree module carries the marker because the shipped lifecycle contract requires it. A
    separate source-checkout suite retains the valid sync, CI-harness, and Git metadata contracts.

    Eight deterministic partitions cover every selected node exactly once. Each runs with two child workers; explicit
    fixture groups stay on one shard and worker so expensive lifecycle setup is shared by its receipt checks.
    Partitioning never changes marker membership or skips Windows cases.

    The outer deadline includes package copying and child cleanup; sharing the child's deadline lets the Windows pytest-
    timeout thread terminate the worker before subprocess.run can reap and report a timed-out selection.

    Flushed phase timings retain completed work and active cases in the timeout failure, including teardown stalls.
    """
    installed_root = _copied_package_root(tmp_path)
    assert not _checkout_context_present(installed_root), "installed payload must not include checkout context"
    assert not (tmp_path / ".git").exists()

    result = _run_package_safe_selection(installed_root, shard)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _checkout_context_present(installed_root), "test execution created checkout context"


def test_installed_package_selection_timeout_is_bounded_for_native_windows() -> None:
    """The child deadline and the outer pytest-timeout leave room for setup and timeout cleanup on native Windows.

    The child limit must exceed a minute and stay within five; the gate test must carry exactly one outer deadline at
    least a minute longer than the child's, so a stalled child is reaped and reported instead of killed mid-reap.
    """
    marks = getattr(test_installed_package_runs_the_explicit_package_safe_selection, "pytestmark", [])
    timeouts = [mark.args[0] for mark in marks if mark.name == "timeout"]

    assert 60 < INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS <= 300
    assert len(timeouts) == 1, "the installed-package gate needs an explicit outer deadline"
    assert INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS + 60 <= timeouts[0] <= 300


@mock.patch("subprocess.run", autospec=True)
def test_installed_package_timeout_prints_partial_streams_and_preserves_exception(
    mock_run: mock.MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A child timeout prints its partial stdout/stderr and re-raises the original ``TimeoutExpired`` unchanged.

    ``subprocess.run`` is the only boundary mocked; it raises a timeout carrying undecodable partial output. The shard
    command shape (two workers, fixture grouping, the child deadline) is the contract the mock records.
    """
    error = subprocess.TimeoutExpired([sys.executable, "-m", "pytest"], 180, b"completed case\n\xff", b"child stderr\n")
    mock_run.side_effect = error

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        _run_package_safe_selection(tmp_path, 0)

    command = mock_run.call_args.args[0]
    assert command[command.index("-n") + 1] == "2"
    assert command[command.index("--dist") + 1] == "loadgroup"
    assert mock_run.call_args.kwargs["timeout"] == INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS
    assert raised.value is error
    output = capsys.readouterr().out
    assert "completed case\n�" in output
    assert "child stderr" in output
