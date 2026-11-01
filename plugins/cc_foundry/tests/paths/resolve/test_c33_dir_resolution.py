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


def _resolve_script(cache_dir: Path) -> str:
    """Return the fixed ``_C33_DIR`` resolution snippet (latest version dir, ``.claude/`` fallback) for a cache dir."""
    return f'_C33_DIR=$(ls -d "{cache_dir}/foundry/"*/ 2>/dev/null | sort -V | tail -1); echo "${{_C33_DIR:-.claude/}}"'


@pytest.fixture
def make_cache(tmp_path: Path):
    """Return a factory creating one empty ``foundry/<version>`` dir per version and yielding the cache dir."""

    def _make(versions: list[str]) -> Path:
        for ver in versions:
            (tmp_path / "foundry" / ver).mkdir(parents=True)
        return tmp_path

    return _make


@_skip_bash_unavailable
class TestFixedResolution:
    """Fixed ``_C33_DIR`` snippet picks one version-specific dir, with a ``.claude/`` fallback."""

    @pytest.mark.parametrize(
        "versions",
        [
            pytest.param(["0.16.0", "0.17.0"], id="two-versions"),
            pytest.param(["0.15.0", "0.16.0", "0.17.0"], id="three-versions"),
        ],
    )
    def test_resolves_to_single_latest_version(self, make_cache, versions: list[str]) -> None:
        """Resolution returns exactly one dir, the highest cached foundry version, never the cache root.

        Several foundry versions sit in the cache after plugin updates; scanning all of them is the B2 defect, so the
        snippet must collapse them to the newest one.
        """
        cache = make_cache(versions)

        result = _bash(_resolve_script(cache))

        resolved = [line for line in result.stdout.strip().splitlines() if line]
        assert result.returncode == 0
        assert len(resolved) == 1, f"Expected a single version dir, got: {resolved}"
        assert "0.17.0" in resolved[0], f"Expected the latest version path, got: {resolved}"

    def test_falls_back_to_claude_dir_when_no_cache(self, tmp_path: Path) -> None:
        """``_C33_DIR`` falls back to ``.claude/`` when no foundry cache exists.

        A fresh machine has no plugin cache; the check must still point at the local ``.claude/`` directory.
        """
        result = _bash(_resolve_script(tmp_path))

        assert result.returncode == 0
        assert result.stdout.strip() == ".claude/"


@_skip_bash_unavailable
def test_old_code_returns_root_not_version(tmp_path: Path) -> None:
    """Documents the OLD buggy behaviour: ``ls -d`` on a dir returns the dir itself.

    The cache root is listed without descending into plugins or versions; this pins why the fixed snippet globs
    ``foundry/*/`` instead.
    """
    root = tmp_path / "borda-ai-rig"
    root.mkdir(parents=True)
    (root / "foundry" / "0.17.0").mkdir(parents=True)
    (root / "oss" / "1.0.0").mkdir(parents=True)

    result = _bash(f'ls -d "{root}/" 2>/dev/null | head -1')

    resolved = result.stdout.strip()
    assert result.returncode == 0
    # Old code returns the root — no version specificity
    assert resolved.rstrip("/").endswith("borda-ai-rig"), (
        f"Old ls -d behavior should return cache root, got: {resolved}"
    )
    assert "0.17.0" not in resolved
