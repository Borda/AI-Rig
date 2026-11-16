"""Census scanner taken from a directory as it stands (``--scanner-dir``), offline: no git checkout, no network."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from _bench_census.run import (
    WORKING_TREE_REF,
    CensusError,
    build_index,
    scanner_directory,
    scanner_tree_digest,
)

BENCHMARKS_DIR = Path(__file__).resolve().parents[2]

#: Fake scanner entry writing one empty index of generation 16 into the working directory.
_FAKE_ENTRY = """\
import json, pathlib
out = pathlib.Path.cwd() / ".cache" / "codemap"
out.mkdir(parents=True)
(out / "repo.json").write_text(json.dumps({"scan_version": 16, "modules": []}))
"""


def _fake_scanner_root(root: Path, scan_version: int = 16) -> Path:
    """Lay out ``plugins/codemap-py`` with an entry point and a schema module under *root*."""
    plugin = root / "plugins" / "codemap-py"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "scripts" / "codemap_py_entry.py").write_text(_FAKE_ENTRY, encoding="utf-8")
    (plugin / "src" / "codemap_py").mkdir(parents=True)
    (plugin / "src" / "codemap_py" / "schema.py").write_text(f"SCAN_VERSION: int = {scan_version}\n", encoding="utf-8")
    return plugin


def _load_cli(name: str):
    """Load a hyphen-named census script as a module."""
    path = BENCHMARKS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scanner_directory_uses_the_tree_in_place_and_names_it_by_digest(tmp_path: Path) -> None:
    """A directory scanner points at the tree as it stands, names it by digest, and removes nothing."""
    plugin = _fake_scanner_root(tmp_path / "tree")
    with scanner_directory(tmp_path / "tree") as scanner:
        assert scanner.ref == WORKING_TREE_REF
        assert scanner.plugin_root == plugin.resolve()
        assert scanner.scan_version == 16
        assert scanner.sha == scanner_tree_digest(plugin)
        assert len(scanner.sha) == 64
    assert (plugin / "scripts" / "codemap_py_entry.py").is_file(), "the directory scanner must not remove the tree"


def test_scanner_directory_feeds_the_index_builder(tmp_path: Path) -> None:
    """The index builder runs a directory scanner's entry point and accepts its generation."""
    _fake_scanner_root(tmp_path / "tree")
    clone = tmp_path / "clone"
    clone.mkdir()
    with scanner_directory(tmp_path / "tree") as scanner:
        index_path = build_index(scanner, clone)
    assert json.loads(index_path.read_text(encoding="utf-8"))["scan_version"] == 16


def test_tree_digest_tracks_source_bytes_and_ignores_bytecode(tmp_path: Path) -> None:
    """The digest changes with source bytes and ignores byte-code caches."""
    plugin = _fake_scanner_root(tmp_path / "tree")
    before = scanner_tree_digest(plugin)
    cache = plugin / "src" / "codemap_py" / "__pycache__"
    cache.mkdir()
    (cache / "schema.py").write_text("ignored\n", encoding="utf-8")
    assert scanner_tree_digest(plugin) == before
    (plugin / "src" / "codemap_py" / "schema.py").write_text("SCAN_VERSION: int = 17\n", encoding="utf-8")
    assert scanner_tree_digest(plugin) != before


def test_scanner_directory_without_a_scanner_fails_with_the_expected_path(tmp_path: Path) -> None:
    """A directory without the scanner entry point fails, naming the missing path."""
    with (
        pytest.raises(CensusError, match=r"plugins/codemap-py/scripts/codemap_py_entry\.py"),
        scanner_directory(tmp_path),
    ):
        pass


@pytest.mark.parametrize(
    "script", [pytest.param("run-import-census", id="import"), pytest.param("run-callgraph-census", id="callgraph")]
)
def test_cli_rejects_a_ref_and_a_directory_together(tmp_path: Path, script: str) -> None:
    """Both census CLIs refuse a revision and a directory at once, before any work starts."""
    cli = _load_cli(script)
    with pytest.raises(CensusError, match="either --scanner-ref or --scanner-dir"):
        cli.main(scanner_ref="HEAD", scanner_dir=str(tmp_path), workdir=str(tmp_path / "work"))
