"""Regression tests for ``rename-refs`` mode files: caller lookup (4c), module destination and package stop (5a)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_MODES_DIR = _PLUGIN_ROOT / "claude-skills" / "rename-refs" / "modes"
_CODEX_SKILL = _PLUGIN_ROOT / "codex-skills" / "rename-refs" / "SKILL.md"
_BASH_BLOCK = re.compile(r"```bash\n(.*?)```", re.DOTALL)


def _bash_runs_posix_script() -> bool:
    """Reject launchers that exist on PATH but cannot execute a POSIX script, including WSL stubs."""
    try:
        result = subprocess.run(["bash", "-c", "printf ok"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout == b"ok"


_needs_git_and_bash = pytest.mark.skipif(
    shutil.which("git") is None or not _bash_runs_posix_script(), reason="needs git and a working POSIX bash"
)


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(_MODES_DIR / "symbol.md", id="claude-symbol-mode"),
        pytest.param(_CODEX_SKILL, id="codex"),
    ],
)
def test_caller_lookup_splits_module_qualified_caller(path: Path) -> None:
    """``fn-rdeps`` callers are ``module::qualified_name``; ``query symbol`` never matches that form.

    Live repro: ``query symbol "pkg.user::K.meth"`` exits 1 with ``Symbol ... not found`` while
    ``query symbols pkg.user`` lists ``K.meth`` with its range, so the lookup must split at ``::``.
    """
    flat = _flat(path.read_text(encoding="utf-8"))

    assert "pass directly to" not in flat
    assert 'query symbol "<caller_qname>"' not in flat
    assert "query symbols" in flat
    assert "::" in flat
    assert "split" in flat


def _module_rename_block() -> str:
    text = (_MODES_DIR / "module.md").read_text(encoding="utf-8")
    return next(block for block in _BASH_BLOCK.findall(text) if 'git mv -- "$old_file_path"' in block)


@_needs_git_and_bash
@pytest.mark.parametrize(
    ("old_file", "old_module", "new_module", "expected"),
    [
        pytest.param("oldtop.py", "oldtop", "newtop", "newtop.py", id="top-level-flat"),
        pytest.param("src/oldtop.py", "oldtop", "newtop", "src/newtop.py", id="top-level-src-layout"),
        pytest.param("src/pkg/old.py", "pkg.old", "pkg.new", "src/pkg/new.py", id="nested-src-layout"),
    ],
)
def test_module_rename_destination_keeps_import_root(
    tmp_path: Path, old_file: str, old_module: str, new_module: str, expected: str
) -> None:
    """A top-level module rename lands beside the source, never at ``.newtop.py`` or ``srcnewtop.py``."""
    repo = tmp_path / "proj"
    (repo / old_file).parent.mkdir(parents=True, exist_ok=True)
    (repo / old_file).write_text("X = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=30)
    subprocess.run(["git", "add", old_file], cwd=repo, check=True, timeout=30)
    sentinels = tmp_path / "tmp"
    sentinels.mkdir()
    prefix, csid = "codemap-proj", "sess1"
    for name, value in {
        "old-file-path": old_file,
        "rename-NEW_REF": new_module,
        "rename-OLD_MODULE_PATH": old_module,
    }.items():
        (sentinels / f"{prefix}-{name}-{csid}").write_text(f"{value}\n", encoding="utf-8")
    env = {
        **os.environ,
        "TMPDIR": str(sentinels),
        "CLAUDE_CODE_SESSION_ID": csid,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_ROOT),
    }

    result = subprocess.run(
        ["bash", "-c", _module_rename_block()], cwd=repo, env=env, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert (repo / expected).is_file()
    assert not (repo / old_file).exists()


def _package_init_block() -> str:
    text = (_MODES_DIR / "module.md").read_text(encoding="utf-8")
    return next(block for block in _BASH_BLOCK.findall(text) if "*/__init__.py|__init__.py)" in block)


@_needs_git_and_bash
def test_package_init_target_stops_with_git_mv_hint(tmp_path: Path) -> None:
    """A module that indexes to a package ``__init__.py`` stops with a ``git mv`` hint built from both dotted refs.

    The new path is read from the ``NEW_REF`` sentinel with ``IFS= read -r`` (it used to be a ``$(cat …)`` capture) and
    both dotted refs become slash paths through parameter expansion.
    """
    repo = tmp_path / "proj"
    (repo / "src" / "pkg" / "sub").mkdir(parents=True)
    (repo / "src" / "pkg" / "sub" / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=30)
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps({"modules": [{"name": "pkg.sub", "path": "src/pkg/sub/__init__.py"}]}), encoding="utf-8"
    )
    sentinels = tmp_path / "tmp"
    sentinels.mkdir()
    prefix, csid = "codemap-proj", "sess1"
    for name, value in {
        "resolve-index": str(index),
        "rename-NEW_REF": "pkg.renamed",
        "rename-OLD_MODULE_PATH": "pkg.sub",
    }.items():
        (sentinels / f"{prefix}-{name}-{csid}").write_text(f"{value}\n", encoding="utf-8")
    env = {
        **os.environ,
        "TMPDIR": str(sentinels),
        "CLAUDE_CODE_SESSION_ID": csid,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_ROOT),
        # the block calls bare ``python``; put this interpreter first so the run never depends on the host PATH
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }

    result = subprocess.run(
        ["bash", "-c", _package_init_block()], cwd=repo, env=env, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 1
    assert "use 'git mv pkg/sub pkg/renamed' directly" in result.stderr
