"""Regression tests for Check 33b non-LOCAL_MODE _C33_DIR resolution (B2 fix).

Old code: ls -d ~/.claude/plugins/cache/borda-ai-rig/ returns the root dir itself — no version specificity, causing grep
to scan all cached versions × all plugins.

Fixed code: ls -d .../foundry/*/ | sort -V | tail -1 resolves to latest version dir only.
"""

import subprocess
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

# Capability probe, not a platform test: a Windows host with Git Bash on PATH runs
# these fine, while the WSL launcher stub (`bash.exe` with no distribution) does not.
BASH_UNAVAILABLE = not _bash_runs_posix_script()
_skip_bash_unavailable = pytest.mark.skipif(
    BASH_UNAVAILABLE,
    reason="`bash` on PATH does not execute POSIX scripts (WSL launcher stub or absent)",
)


def _bash(script: str) -> subprocess.CompletedProcess:
    """Run one Bash fixture script and capture its completed process."""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


@_skip_bash_unavailable
def test_fixed_resolution_finds_latest_version(tmp_path: Path) -> None:
    """Fixed _C33_DIR resolves to latest foundry version dir, not cache root."""
    v1 = tmp_path / "foundry" / "0.16.0"
    v2 = tmp_path / "foundry" / "0.17.0"
    v1.mkdir(parents=True)
    v2.mkdir(parents=True)

    result = _bash(
        f'_C33_DIR=$(ls -d "{tmp_path}/foundry/"*/ 2>/dev/null | sort -V | tail -1); echo "${{_C33_DIR:-.claude/}}"'
    )
    assert result.returncode == 0
    resolved = result.stdout.strip()
    assert "0.17.0" in resolved, f"Expected version-specific path, got: {resolved}"


@_skip_bash_unavailable
def test_fixed_resolution_excludes_older_versions(tmp_path: Path) -> None:
    """Fixed resolution returns exactly one version, not all of them."""
    for ver in ["0.15.0", "0.16.0", "0.17.0"]:
        (tmp_path / "foundry" / ver).mkdir(parents=True)

    result = _bash(f'ls -d "{tmp_path}/foundry/"*/ 2>/dev/null | sort -V | tail -1')
    assert result.returncode == 0
    lines = [line for line in result.stdout.strip().splitlines() if line]
    assert len(lines) == 1, f"Should resolve to single version dir, got: {lines}"


@_skip_bash_unavailable
def test_old_code_returns_root_not_version(tmp_path: Path) -> None:
    """Documents OLD buggy behavior: ls -d on a dir returns the dir itself."""
    root = tmp_path / "borda-ai-rig"
    root.mkdir(parents=True)
    (root / "foundry" / "0.17.0").mkdir(parents=True)
    (root / "oss" / "1.0.0").mkdir(parents=True)

    result = _bash(f'ls -d "{root}/" 2>/dev/null | head -1')
    assert result.returncode == 0
    resolved = result.stdout.strip()
    # Old code returns the root — no version specificity
    assert resolved.rstrip("/").endswith("borda-ai-rig"), (
        f"Old ls -d behavior should return cache root, got: {resolved}"
    )
    assert "0.17.0" not in resolved


@_skip_bash_unavailable
def test_fallback_when_no_cache(tmp_path: Path) -> None:
    """_C33_DIR falls back to .claude/ when cache absent."""
    result = _bash(
        f'_C33_DIR=$(ls -d "{tmp_path}/foundry/"*/ 2>/dev/null | sort -V | tail -1); echo "${{_C33_DIR:-.claude/}}"'
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ".claude/"
