"""Tests for ``bin/resolve-quality-gates.sh`` quality-gates.md path resolver.

The script resolves foundry's ``quality-gates.md`` by checking the project-local ``.claude/rules/`` directory first,
then the foundry plugin cache. Exits 0 with the resolved path on stdout when found, or exit 1 with a stderr warning when
neither location yields a hit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parent.parent / "bin" / "resolve-quality-gates.sh"


def _sh(*args: str, env: dict | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run the script under test and capture stdout/stderr."""
    e = {**os.environ, **(env or {})}
    for name in ("HOME", "GIT_ROOT"):
        if name in e:
            e[name] = Path(e[name]).as_posix()
    return subprocess.run(
        ["bash", SCRIPT.as_posix(), *args],
        capture_output=True,
        text=True,
        env=e,
        cwd=cwd,
    )


def _bash_available() -> bool:
    """Probe whether Bash executes commands instead of merely existing on PATH."""
    try:
        result = subprocess.run(["bash", "-c", "printf ok"], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout == "ok"


_skip_bash_unavailable = pytest.mark.skipif(not _bash_available(), reason="requires working bash")


@_skip_bash_unavailable
def test_local_claude_rules_preferred(tmp_path: Path) -> None:
    """Project-local ``.claude/rules/quality-gates.md`` takes priority over cache."""
    project = tmp_path / "project"
    rules = project / ".claude" / "rules"
    rules.mkdir(parents=True)
    local_file = rules / "quality-gates.md"
    local_file.write_text("# local rules\n")

    # Also stage a cache hit; local must still win.
    cache_dir = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry" / "0.1.0" / "rules"
    cache_dir.mkdir(parents=True)
    (cache_dir / "quality-gates.md").write_text("# cached rules\n")

    result = _sh(env={"HOME": str(tmp_path), "GIT_ROOT": str(project)}, cwd=str(project))
    assert result.returncode == 0
    assert result.stdout.strip() == local_file.as_posix()


@_skip_bash_unavailable
def test_cache_fallback_when_local_absent(tmp_path: Path) -> None:
    """No local ``.claude/rules/`` → resolver falls back to foundry plugin cache."""
    project = tmp_path / "project"
    project.mkdir()

    cache_file = (
        tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry" / "0.1.0" / "rules" / "quality-gates.md"
    )
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("# cached rules\n")

    result = _sh(env={"HOME": str(tmp_path), "GIT_ROOT": str(project)}, cwd=str(project))
    assert result.returncode == 0
    assert result.stdout.strip() == cache_file.as_posix()


@_skip_bash_unavailable
def test_neither_location_exits_nonzero(tmp_path: Path) -> None:
    """No local and no cached file → exit 1 with stderr warning, empty stdout."""
    project = tmp_path / "project"
    project.mkdir()

    result = _sh(env={"HOME": str(tmp_path), "GIT_ROOT": str(project)}, cwd=str(project))
    assert result.returncode == 1
    assert result.stdout.strip() == ""
    assert "quality-gates.md not found" in result.stderr


@_skip_bash_unavailable
def test_git_root_env_override(tmp_path: Path) -> None:
    """Prefer an explicit repository root over Git discovery."""
    explicit_root = tmp_path / "explicit"
    rules = explicit_root / ".claude" / "rules"
    rules.mkdir(parents=True)
    local_file = rules / "quality-gates.md"
    local_file.write_text("# explicit root rules\n")

    # cwd is unrelated; GIT_ROOT must win.
    result = _sh(env={"HOME": str(tmp_path), "GIT_ROOT": str(explicit_root)}, cwd=str(tmp_path))
    assert result.returncode == 0
    assert result.stdout.strip() == local_file.as_posix()
