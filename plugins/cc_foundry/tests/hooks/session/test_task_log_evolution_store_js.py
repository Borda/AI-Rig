"""Contract test: the SessionEnd artifact sweep never reaches the durable ``.evolution/`` scan store.

``/oss:analyse evolution`` keeps a resumable scan store under ``.evolution/<host>/<owner>/<repo>/`` (a dedicated clone,
``overrides.csv`` with the user's corrections, ``adjudications.jsonl`` with paid LLM verdicts, the lock, and a
regenerable ``store/``). ``task-log.js`` deletes expired files under ``.cache``, ``.temp`` and ``.plans/blueprint`` at
every SessionEnd, so a store placed or later added under one of those roots would lose its corrections after 30 days.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git executable not available")
_requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node executable not available")

_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

#: Age of every planted file, well past the 30-day retention the sweep applies to the roots it owns.
_EXPIRED_SECONDS = 40 * 24 * 60 * 60

#: Store layout from ``modes/evolution.md``, relative to ``.evolution/<host>/<owner>/<repo>/``.
_STORE_FILES = (
    "store/manifest.json",
    "store/rows.jsonl",
    "overrides.csv",
    "adjudications.jsonl",
    ".lock",
    "repo/README.md",
)
_STORE_ROOT = Path(".evolution") / "github.com" / "owner" / "repo"


def _git(cwd: Path, *args: str) -> None:
    """Run one git command in ``cwd`` isolated from user and system config, failing the test on error."""
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        env=_GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    )


def _expire(path: Path) -> None:
    """Backdate the modification time of ``path`` to the expired age."""
    stamp = time.time() - _EXPIRED_SECONDS
    os.utime(path, (stamp, stamp))


@pytest.fixture(name="home")
def _home(tmp_path: Path) -> Path:
    """Return an isolated HOME so the hook's audit-log writes stay out of the real user directory."""
    home = tmp_path / "home"
    (home / ".claude" / "logs").mkdir(parents=True)
    return home


@pytest.fixture(name="project")
def _project(tmp_path: Path) -> Path:
    """Build a git project holding an expired scan store and an expired ``.temp`` file of the same age.

    The ``.temp`` file is the control: it lives under a root the sweep owns, so it must disappear in the same run in
    which the store survives. A store that survived only because the sweep never ran would otherwise pass.
    """
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    (project / "a.txt").write_text("a\n", encoding="utf-8")
    _git(project, "add", "a.txt")
    _git(project, "commit", "-q", "-m", "init")
    for relative in _STORE_FILES:
        target = project / _STORE_ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{relative}\n", encoding="utf-8")
    (project / ".temp").mkdir()
    (project / ".temp" / "control.md").write_text("expired\n", encoding="utf-8")
    for path in sorted((project / ".evolution").rglob("*"), reverse=True):
        _expire(path)
    _expire(project / ".evolution")
    _expire(project / ".temp" / "control.md")
    _expire(project / ".temp")
    return project


@pytest.mark.integration
@_requires_git
@_requires_node
class TestSessionEndKeepsEvolutionStore:
    """task-log.js SessionEnd never deletes the durable ``/oss:analyse evolution`` store."""

    def test_expired_store_survives_while_expired_temp_file_is_pruned(
        self, sid: str, home: Path, project: Path, run_hook
    ) -> None:
        """A 40-day-old store keeps every file after SessionEnd, while an equally old ``.temp`` file is deleted.

        The store holds work that cannot be regenerated: the user's ``overrides.csv`` corrections and the saved LLM
        verdicts. The ``.temp`` control proves the sweep ran and would have removed a file of that age.
        """
        result = run_hook("task-log.js", {"hook_event_name": "SessionEnd", "session_id": sid}, home=home, cwd=project)

        assert result.returncode == 0, result.stderr
        missing = [relative for relative in _STORE_FILES if not (project / _STORE_ROOT / relative).is_file()]
        assert (missing, (project / ".temp" / "control.md").exists()) == ([], False)

    def test_store_content_is_unchanged(self, sid: str, home: Path, project: Path, run_hook) -> None:
        """The sweep leaves the bytes of a stored correction untouched, not only its existence."""
        corrections = project / _STORE_ROOT / "overrides.csv"
        before = corrections.read_bytes()

        result = run_hook("task-log.js", {"hook_event_name": "SessionEnd", "session_id": sid}, home=home, cwd=project)

        assert (result.returncode, corrections.read_bytes()) == (0, before)
