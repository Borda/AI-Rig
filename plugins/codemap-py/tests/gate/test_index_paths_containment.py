"""Containment of an index or log directory inside its project root, including Windows extended-length spellings."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from codemap_py import index_paths


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("\\\\?\\C:\\proj\\.cache", "C:\\proj\\.cache", id="drive"),
        pytest.param("\\\\?\\UNC\\host\\share\\p", "\\\\host\\share\\p", id="unc"),
        pytest.param("\\\\?\\Volume{1234}\\p", "\\\\?\\Volume{1234}\\p", id="volume-guid-kept"),
        pytest.param("C:\\proj", "C:\\proj", id="plain-drive"),
        pytest.param("/srv/proj", "/srv/proj", id="posix"),
    ],
)
def test_without_extended_prefix_only_unwraps_drive_and_unc_spellings(text: str, expected: str) -> None:
    """A drive or UNC spelling loses the prefix; every other string stays as it is."""
    assert index_paths.without_extended_prefix(text) == expected


def test_is_within_accepts_a_directory_that_does_not_exist_yet(tmp_path: Path) -> None:
    """The index directory is tested before its first writer creates it."""
    assert index_paths.is_within(tmp_path / ".cache" / "codemap", tmp_path)


def test_is_within_refuses_the_parent_and_a_sibling(tmp_path: Path) -> None:
    """A path above the root, or beside it, is outside."""
    root = tmp_path / "proj"
    root.mkdir()
    (tmp_path / "other").mkdir()
    assert not index_paths.is_within(tmp_path, root)
    assert not index_paths.is_within(tmp_path / "other", root)


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs a privilege on Windows")
def test_is_within_refuses_a_symlink_leading_out_of_the_root(tmp_path: Path) -> None:
    """A ``.cache`` link pointing elsewhere is an escape, whatever its spelling."""
    root, elsewhere = tmp_path / "proj", tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()
    (root / ".cache").symlink_to(elsewhere, target_is_directory=True)
    assert not index_paths.is_within(root / ".cache" / "codemap", root)
