"""Tests for ``bin/resolve_proj_index.py`` — PROJ name + INDEX path resolver.

Covers:
* ``compute_proj_index()`` uses git root basename when inside a repo
* Falls back to CWD basename outside a repo
* INDEX path is ``<root>/.cache/codemap/<proj>.json`` by default
* ``CODEMAP_INDEX_DIR`` env var overrides the index directory
* ``--check`` exits 1 when index absent; exits 0 and prints ✓ when present
* ``main()`` without ``--check`` always exits 0
* Windows-portability invariants: no ``/tmp``, ``shell=True`` absent

The git lookup is the only external boundary: one test per behaviour that depends on it runs the real ``git``, the
rest replace ``subprocess.run`` so path and exit-code logic is exercised without spawning a process.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest import mock

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "bin" / "resolve_proj_index.py"
_spec = importlib.util.spec_from_file_location("codemap_resolve_proj_index", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

compute_proj_index = _mod.compute_proj_index
main = _mod.main

# Reusable decorator: ``git rev-parse`` fails, as outside a repository or without git installed.
_git_unavailable = mock.patch.object(
    _mod.subprocess, "run", autospec=True, side_effect=subprocess.CalledProcessError(128, ["git"])
)


@pytest.fixture(name="work")
def _work(tmp_path: Path) -> Path:
    """Create an empty project directory named ``my-project`` and return it."""
    work = tmp_path / "my-project"
    work.mkdir()
    return work


@pytest.fixture(name="work_cwd")
def _work_cwd(work: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make the ``my-project`` directory the working directory and return it."""
    monkeypatch.chdir(work)
    return work


class TestComputeProjIndex:
    """Resolve the project name and index path from a working directory."""

    def test_cwd_fallback_uses_basename(self, work: Path) -> None:
        """Falls back to the CWD basename outside a git repository (real ``git rev-parse`` fails there)."""
        proj, _ = compute_proj_index(cwd=work)

        assert proj == "my-project"

    @_git_unavailable
    def test_git_failure_falls_back_to_cwd(self, mock_run: mock.Mock, work: Path) -> None:
        """Falls back to CWD basename when the git subprocess raises ``CalledProcessError``."""
        proj, _ = compute_proj_index(cwd=work)

        assert proj == "my-project"
        mock_run.assert_called_once()

    @_git_unavailable
    def test_index_path_structure(self, mock_run: mock.Mock, work: Path) -> None:
        """Index path is ``<root>/.cache/codemap/<proj>.json`` by default."""
        proj, index = compute_proj_index(cwd=work)

        assert index == work / ".cache" / "codemap" / f"{proj}.json"

    @_git_unavailable
    def test_codemap_index_dir_env_overrides_path(
        self, mock_run: mock.Mock, work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Relocate the index through the configured index directory."""
        custom_dir = tmp_path / "custom-cache"
        monkeypatch.setenv("CODEMAP_INDEX_DIR", str(custom_dir))

        proj, index = compute_proj_index(cwd=work)

        assert index == custom_dir / f"{proj}.json"
        assert ".cache" not in str(index)

    @mock.patch.object(_mod.subprocess, "run", autospec=True)
    def test_git_root_basename_used_when_available(self, mock_run: mock.Mock, tmp_path: Path) -> None:
        """Uses the git root's directory name, not the CWD's, when ``git rev-parse`` returns a path.

        Run from a subdirectory of the repository, the project is still named after the repository root and the index
        sits under that root.
        """
        repo_root = tmp_path / "my-repo"
        sub = repo_root / "subdir"
        sub.mkdir(parents=True)
        mock_run.return_value = subprocess.CompletedProcess(["git"], 0, stdout=f"{repo_root}\n", stderr="")

        proj, index = compute_proj_index(cwd=sub)

        assert proj == "my-repo"
        assert index == repo_root / ".cache" / "codemap" / "my-repo.json"

    def test_real_git_root_basename_used(self, tmp_path: Path) -> None:
        """A real ``git init`` repository names the project after its root even from a subdirectory.

        Keeps one un-mocked ``git rev-parse --show-toplevel`` call so the mocked cases above cannot drift from git's
        real output format.
        """
        repo_root = tmp_path / "real-repo"
        sub = repo_root / "subdir"
        sub.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo_root)], check=True, capture_output=True)

        proj, _ = compute_proj_index(cwd=sub)

        assert proj == "real-repo"


class TestMain:
    """``main()`` entry point: exit codes and output of the resolver and its ``--check`` mode."""

    @_git_unavailable
    def test_no_check_always_exits_0(self, mock_run: mock.Mock, work_cwd: Path) -> None:
        """Without ``--check``, main always exits 0 even though no index exists."""
        assert main([]) == 0

    @_git_unavailable
    def test_check_exits_1_when_index_absent(
        self, mock_run: mock.Mock, work_cwd: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Report a missing project index as a failure.

        ``--check`` is the skill's gate: exit 1 and a ``✗`` line mean the index must be built first.
        """
        rc = main(["--check"])

        assert rc == 1
        assert "✗" in capsys.readouterr().out

    @_git_unavailable
    def test_check_exits_0_when_index_present(
        self, mock_run: mock.Mock, work_cwd: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Report an existing project index as valid with a ``✓`` line and exit 0."""
        index = work_cwd / ".cache" / "codemap" / "my-project.json"
        index.parent.mkdir(parents=True)
        index.write_text("{}", encoding="utf-8")

        rc = main(["--check"])

        assert rc == 0
        assert "✓" in capsys.readouterr().out

    @_git_unavailable
    def test_output_has_no_crlf(self, mock_run: mock.Mock, work_cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Stdout must not contain CRLF (Windows text-mode regression guard)."""
        main([])

        assert "\r" not in capsys.readouterr().out

    @mock.patch.object(
        _mod.Path, "expanduser", autospec=True, side_effect=RuntimeError("Could not determine home directory.")
    )
    def test_unresolvable_index_dir_exits_1(
        self, mock_expanduser: mock.Mock, work_cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """``CODEMAP_INDEX_DIR`` naming an unknown ``~user`` is reported with a ``!`` line and exit 1, not a traceback.

        ``Path.expanduser`` fails the way it does when the user's home directory cannot be determined.
        """
        monkeypatch.setenv("CODEMAP_INDEX_DIR", "~no-such-user-codemap/cache")

        rc = main([])

        assert rc == 1
        assert capsys.readouterr().out.startswith("! CODEMAP_INDEX_DIR cannot be resolved")
