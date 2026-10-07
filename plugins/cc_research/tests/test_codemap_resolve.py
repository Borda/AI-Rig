"""Regression tests for cc_research's codemap gate wiring.

``bin/codemap_resolve.py`` is byte-identical to cc_develop's canonical copy, so the resolver's own behaviour is pinned
once on the canonical side. What must be pinned *here* is that this plugin's shipped copy really is that file and that
research's own wrapper supplies research's sentinel name — the only per-plugin difference left.

These are re-asserted against the shipped copy rather than trusted transitively: the byte-identity check and these
behavioural checks fail independently, so a bad propagation cannot pass by matching a stale canonical.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RESEARCH_BIN = _REPO_ROOT / "plugins" / "cc_research" / "bin"
_RESEARCH_RESOLVER = _RESEARCH_BIN / "codemap_resolve.py"
_DEV_RESOLVER = _REPO_ROOT / "plugins" / "cc_develop" / "bin" / "codemap_resolve.py"


def _load(path: Path, name: str) -> ModuleType:
    """Load *path* under a unique module name — both plugins ship a ``codemap_resolve``."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolver = _load(_RESEARCH_RESOLVER, "research_codemap_resolve_under_test")


def _git_repo(tmp_path: Path, name: str) -> Path:
    """Create an initialized git repository named *name* under *tmp_path*."""
    root = tmp_path / name
    (root / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def test_shipped_copy_matches_the_canonical_resolver():
    """Cc_develop is canonical; drift here is what the MANIFEST entry must prevent."""
    assert _RESEARCH_RESOLVER.read_bytes() == _DEV_RESOLVER.read_bytes()


def test_index_is_found_from_a_repository_subdirectory(tmp_path, monkeypatch):
    """Research skills run from wherever the session sits; the index lives at the root."""
    root = _git_repo(tmp_path, "proj")
    index = root / ".cache" / "codemap" / "proj.json"
    index.parent.mkdir(parents=True)
    index.write_text("{}")
    monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
    monkeypatch.chdir(root / "sub")

    assert resolver._index_path(resolver._canonical_root()) == index.resolve()


@pytest.mark.parametrize("name", ["café", "my+proj", "two words"])
def test_project_name_is_the_raw_basename(tmp_path, monkeypatch, name):
    """Sanitizing here sought a filename the scanner never writes."""
    root = _git_repo(tmp_path, name)
    monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
    monkeypatch.chdir(root)

    assert resolver._index_path(resolver._canonical_root()).name == f"{name}.json"


def test_retired_plugin_label_is_gone():
    """This copy had drifted to the retired bare `codemap` label in user-facing text."""
    assert resolver.TOOL_LABEL == "codemap-py"
    assert not hasattr(resolver, "QUERY_LABEL")


def test_research_wrapper_supplies_the_research_prefix(monkeypatch):
    """The sentinel name is research's concern and lives in research's own wrapper."""
    flag = _load(_RESEARCH_BIN / "codemap-flag.py", "research_codemap_flag_under_test")
    captured: dict[str, list[str]] = {}

    def _fake_run(cmd, **kwargs):
        """Return the wrapper's successful boolean response and record argv."""
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="true\n")

    monkeypatch.setattr(flag.subprocess, "run", _fake_run)
    flag._run_resolver("auto", "sess")

    argv = captured["cmd"]
    assert flag.CURRENCY_PREFIX == "research-codemap-currency"
    assert argv[argv.index("--currency-prefix") + 1] == "research-codemap-currency"
    assert argv[0] == sys.executable


def test_wrapper_prefix_matches_the_sentinel_the_gates_read_back():
    """The resolver writes and codemap-gates.md reads the same name, or the stale gate is blind."""
    flag = _load(_RESEARCH_BIN / "codemap-flag.py", "research_codemap_flag_sentinel_check")
    gates = (_REPO_ROOT / "plugins" / "cc_research" / "skills" / "_shared" / "codemap-gates.md").read_text(
        encoding="utf-8"
    )

    assert f"{flag.CURRENCY_PREFIX}-${{CSID}}" in gates
