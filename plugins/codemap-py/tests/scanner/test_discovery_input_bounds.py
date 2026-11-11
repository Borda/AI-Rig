"""Keep scanner reads bounded and configured paths inside the project."""

from __future__ import annotations

import hashlib
import json
import tracemalloc
from pathlib import Path

import pytest
from _host_skips import skip_file_symlink_unavailable
from codemap_py.scanner.discovery import _detect_src_root_from_config, _working_tree_file_hashes
from codemap_py.scanner.exclusions import Exclusions
from codemap_py.scanner.test_tooling import _collect_module_aliases, extract_conftest_syspath


@pytest.mark.parametrize(
    ("algorithm", "git_blob"),
    [
        pytest.param("sha1", True, id="git-sha1"),
        pytest.param("sha256", True, id="git-sha256"),
        pytest.param("md5", False, id="legacy-md5"),
    ],
)
def test_large_source_hashing_uses_bounded_memory(tmp_path: Path, algorithm: str, git_blob: bool) -> None:
    """Hash source larger than the parser cap without allocating its full contents."""
    content = b"# generated\n" * (1024 * 1024)
    (tmp_path / "generated.py").write_bytes(content)
    expected = hashlib.new(algorithm, usedforsecurity=False)
    expected.update(f"blob {len(content)}\0".encode("ascii") if git_blob else b"")
    expected.update(content)
    tracemalloc.start()
    try:
        hashes = _working_tree_file_hashes(tmp_path, Exclusions(frozenset(), (), {}), algorithm, git_blob=git_blob)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert hashes == {"generated.py": expected.hexdigest()}
    assert peak < 4 * 1024 * 1024


@pytest.mark.parametrize("entry", ["../outside", "inside/../../outside"])
def test_config_source_root_cannot_escape_project(tmp_path: Path, entry: str) -> None:
    """Reject both direct traversal and traversal hidden after an in-project prefix."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "inside").mkdir()
    (tmp_path / "outside").mkdir()
    (root / "pyproject.toml").write_text(f"[tool.setuptools.packages.find]\nwhere = [{json.dumps(entry)}]\n")
    assert _detect_src_root_from_config(root) is None


def test_absolute_external_source_root_is_rejected(tmp_path: Path) -> None:
    """Do not accept a host directory named by an absolute configuration entry."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "setup.cfg").write_text(f"[options.packages.find]\nwhere = [{json.dumps(outside.as_posix())}]\n")
    assert _detect_src_root_from_config(root) is None


def test_local_source_root_remains_discoverable(tmp_path: Path) -> None:
    """Preserve valid configured package roots inside the project."""
    source = tmp_path / "library"
    source.mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.setuptools.packages.find]\nwhere = ["library"]\n')
    assert _detect_src_root_from_config(tmp_path) == source


@skip_file_symlink_unavailable
def test_symlinked_source_root_is_not_followed_outside_project(tmp_path: Path) -> None:
    """Resolve configured directories before deciding that they belong to the project."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "library").symlink_to(outside, target_is_directory=True)
    (root / "pyproject.toml").write_text('[tool.setuptools.packages.find]\nwhere = ["library"]\n')
    assert _detect_src_root_from_config(root) is None


@skip_file_symlink_unavailable
@pytest.mark.parametrize("config_name", ["pyproject.toml", "setup.cfg"])
def test_symlinked_source_configuration_is_not_read(tmp_path: Path, config_name: str) -> None:
    """Ignore configuration symlinks even when the referenced package path is valid."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "library").mkdir()
    outside = tmp_path / "private-config"
    outside.write_text('where = ["library"]\n')
    (root / config_name).symlink_to(outside)
    assert _detect_src_root_from_config(root) is None


@skip_file_symlink_unavailable
def test_symlinked_ignore_file_is_not_hashed(tmp_path: Path) -> None:
    """Keep external ignore-file bytes out of the source fingerprint."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "private-config"
    outside.write_text("private-exclusion\n")
    (root / ".codemapignore").symlink_to(outside)
    assert _working_tree_file_hashes(root, Exclusions(frozenset(), (), {}), "md5", git_blob=False) == {}


@pytest.mark.parametrize(
    "statement",
    [
        'sys.path.insert(0, "../outside")',
        'sys.path.insert(0, str(Path(__file__).parent / "../outside"))',
    ],
)
def test_conftest_rejects_external_import_roots(tmp_path: Path, statement: str) -> None:
    """Do not enumerate host modules through either supported conftest path syntax."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("VALUE = 1\n")
    conftest = root / "conftest.py"
    conftest.write_text(f"import sys\nfrom pathlib import Path\n{statement}\n")
    assert extract_conftest_syspath(conftest, root) == []
    assert _collect_module_aliases([conftest], {"pkg.secret"}, root) == {}


@skip_file_symlink_unavailable
def test_conftest_rejects_external_symlink_import_root(tmp_path: Path) -> None:
    """Reject a lexically internal import directory whose target lies outside the project."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("VALUE = 1\n")
    (root / "library").symlink_to(outside, target_is_directory=True)
    conftest = root / "conftest.py"
    conftest.write_text('import sys\nsys.path.insert(0, "library")\n')
    assert extract_conftest_syspath(conftest, root) == []
    assert _collect_module_aliases([conftest], {"pkg.secret"}, root) == {}


def test_conftest_retains_local_import_root(tmp_path: Path) -> None:
    """Keep module aliases from a valid in-project conftest shim."""
    library = tmp_path / "library"
    library.mkdir()
    (library / "helper.py").write_text("VALUE = 1\n")
    conftest = tmp_path / "conftest.py"
    conftest.write_text('import sys\nsys.path.insert(0, "library")\n')
    assert extract_conftest_syspath(conftest, tmp_path) == [library]
    assert _collect_module_aliases([conftest], {"pkg.helper"}, tmp_path) == {"helper": "pkg.helper"}


@skip_file_symlink_unavailable
def test_unresolvable_source_root_is_ignored(tmp_path: Path) -> None:
    """Skip a cyclic configuration target instead of crashing while checking containment."""
    (tmp_path / "library").symlink_to("library", target_is_directory=True)
    (tmp_path / "pyproject.toml").write_text('where = ["library"]\n')
    assert _detect_src_root_from_config(tmp_path) is None
