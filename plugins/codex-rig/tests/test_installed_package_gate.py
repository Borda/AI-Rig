"""Executable acceptance contract for the installed Codex Rig package payload."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SAFE_TEST_MARKER = "installed_plugin"
INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS = 180


def test_installed_package_selection_timeout_is_bounded_for_native_windows() -> None:
    """Keep the installed lifecycle budget finite while allowing Windows Git setup."""
    assert 60 < INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS <= 300


def _package_payload_paths() -> tuple[str, ...]:
    """Return the exact shipped payload paths in manifest order.

    Example:
        >>> _package_payload_paths()[0]
        '.codex-plugin/plugin.json'
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
def test_installed_package_runs_the_explicit_package_safe_selection(tmp_path: Path) -> None:
    """Prevent checkout-only tests from being mistaken for installed-package coverage.

    The marker selects staged execution manifests, privacy-minimized telemetry, the complete parallel-write lifecycle,
    the denial protocol/client, all seven network approval briefs, both PR collector boundaries, and calibration
    scoring. The complete worktree module carries the marker because the shipped lifecycle contract requires it. A
    separate source-checkout suite retains the valid sync, CI-harness, and Git metadata contracts.
    """
    installed_root = _copied_package_root(tmp_path)
    for path in (installed_root / "Makefile", installed_root / ".github", installed_root / ".git"):
        assert not path.exists(), f"installed payload must not include checkout context: {path.name}"
    assert not (tmp_path / ".git").exists()

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--strict-markers", "-m", PACKAGE_SAFE_TEST_MARKER],
        cwd=installed_root,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=INSTALLED_PACKAGE_SELECTION_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    for path in (installed_root / "Makefile", installed_root / ".github", installed_root / ".git"):
        assert not path.exists(), f"test execution created checkout context: {path.name}"
