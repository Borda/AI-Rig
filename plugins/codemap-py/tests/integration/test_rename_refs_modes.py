"""Regression tests for ``rename-refs`` mode files: caller lookup (4c), module destination and package stop (5a)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from _codemap_skill_text import flat
from _host_skips import needs_git_and_bash

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_MODES_DIR = _PLUGIN_ROOT / "claude-skills" / "rename-refs" / "modes"
_CODEX_SKILL = _PLUGIN_ROOT / "codex-skills" / "rename-refs" / "SKILL.md"
_SENTINEL_PREFIX = "codemap-proj"
_CSID = "sess1"


def _bash_block_containing(path: Path, marker: str) -> str:
    """Return the first fenced ``bash`` block of *path* that contains *marker*, scanning line by line."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        if current is None:
            if line.strip() == "```bash":
                current = []
        elif line.strip() == "```":
            blocks.append(current)
            current = None
        else:
            current.append(line)
    return next(block for block in ("".join(lines) for lines in blocks) if marker in block)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Return an empty git project directory ``proj``."""
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=30)
    return repo


@pytest.fixture
def run_block(tmp_path: Path, git_repo: Path) -> Callable[[str, dict[str, str]], subprocess.CompletedProcess[str]]:
    """Return a runner that writes the given sentinels, then runs a skill bash block in ``git_repo``."""
    sentinels = tmp_path / "tmp"
    sentinels.mkdir()
    env = {
        **os.environ,
        "TMPDIR": str(sentinels),
        "CLAUDE_CODE_SESSION_ID": _CSID,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_ROOT),
        # the block calls bare ``python``; put this interpreter first so the run never depends on the host PATH
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }

    def run(block: str, values: dict[str, str]) -> subprocess.CompletedProcess[str]:
        for name, value in values.items():
            (sentinels / f"{_SENTINEL_PREFIX}-{name}-{_CSID}").write_text(f"{value}\n", encoding="utf-8")
        return subprocess.run(["bash", "-c", block], cwd=git_repo, env=env, capture_output=True, text=True, timeout=60)

    return run


class TestCallerLookup:
    """Mode text for resolving a caller found through ``fn-rdeps``."""

    @pytest.mark.parametrize(
        "path",
        [pytest.param(_MODES_DIR / "symbol.md", id="claude-symbol-mode"), pytest.param(_CODEX_SKILL, id="codex")],
    )
    def test_splits_module_qualified_caller(self, path: Path) -> None:
        """The mode tells the agent to split ``module::qualified_name`` callers at ``::`` and use ``query symbols``.

        ``fn-rdeps`` callers are ``module::qualified_name``; ``query symbol`` never matches that form. Live repro:
        ``query symbol "pkg.user::K.meth"`` exits 1 with ``Symbol ... not found`` while ``query symbols pkg.user``
        lists ``K.meth`` with its range, so the lookup must split at ``::``.
        """
        text = flat(path.read_text(encoding="utf-8"))

        assert "pass directly to" not in text
        assert 'query symbol "<caller_qname>"' not in text
        assert "query symbols" in text
        assert "::" in text
        assert "split" in text


@needs_git_and_bash
class TestModuleMode:
    """The real module-mode bash blocks run against a throwaway git repository."""

    @pytest.mark.parametrize(
        ("old_file", "old_module", "new_module", "expected"),
        [
            pytest.param("oldtop.py", "oldtop", "newtop", "newtop.py", id="top-level-flat"),
            pytest.param("src/oldtop.py", "oldtop", "newtop", "src/newtop.py", id="top-level-src-layout"),
            pytest.param("src/pkg/old.py", "pkg.old", "pkg.new", "src/pkg/new.py", id="nested-src-layout"),
        ],
    )
    def test_rename_destination_keeps_import_root(
        self, git_repo: Path, run_block, old_file: str, old_module: str, new_module: str, expected: str
    ) -> None:
        """A top-level module rename lands beside the source, never at ``.newtop.py`` or ``srcnewtop.py``.

        A tracked source file is renamed through the ``git mv`` block for flat, ``src`` and nested-``src`` layouts; the
        destination must keep the source directory, which the dotted-ref to path conversion previously dropped.
        """
        (git_repo / old_file).parent.mkdir(parents=True, exist_ok=True)
        (git_repo / old_file).write_text("X = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", old_file], cwd=git_repo, check=True, timeout=30)
        block = _bash_block_containing(_MODES_DIR / "module.md", 'git mv -- "$old_file_path"')

        result = run_block(
            block, {"old-file-path": old_file, "rename-NEW_REF": new_module, "rename-OLD_MODULE_PATH": old_module}
        )

        assert result.returncode == 0, result.stderr
        assert (git_repo / expected).is_file()
        assert not (git_repo / old_file).exists()

    def test_package_init_target_stops_with_git_mv_hint(self, tmp_path: Path, git_repo: Path, run_block) -> None:
        """A module that indexes to a package ``__init__.py`` stops with a ``git mv`` hint built from both dotted refs.

        The new path is read from the ``NEW_REF`` sentinel with ``IFS= read -r`` (it used to be a ``$(cat …)`` capture)
        and both dotted refs become slash paths through parameter expansion.
        """
        (git_repo / "src" / "pkg" / "sub").mkdir(parents=True)
        (git_repo / "src" / "pkg" / "sub" / "__init__.py").write_text("", encoding="utf-8")
        index = tmp_path / "index.json"
        index.write_text(
            json.dumps({"modules": [{"name": "pkg.sub", "path": "src/pkg/sub/__init__.py"}]}), encoding="utf-8"
        )
        block = _bash_block_containing(_MODES_DIR / "module.md", "*/__init__.py|__init__.py)")

        result = run_block(
            block,
            {"resolve-index": str(index), "rename-NEW_REF": "pkg.renamed", "rename-OLD_MODULE_PATH": "pkg.sub"},
        )

        assert result.returncode == 1
        assert "use 'git mv pkg/sub pkg/renamed' directly" in result.stderr
