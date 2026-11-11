"""Keep scanner configuration and Markdown references within project input boundaries."""

from pathlib import Path

import pytest
from _host_skips import skip_file_symlink_unavailable
from codemap_py.scanner.docs_xrefs import _MKDOCS_NAMED_RE, scan_config_refs, scan_mkdocs_xrefs
from codemap_py.scanner.exclusions import _load_exclusions, load_src_roots


@skip_file_symlink_unavailable
@pytest.mark.parametrize("config_name", ["pyproject.toml", "setup.cfg", "setup.py", "config.yml", "config.yaml"])
def test_config_references_ignore_symlinked_files(tmp_path: Path, config_name: str) -> None:
    """A configuration symlink cannot copy an external credential line into reference context."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "credentials"
    outside.write_text("private.module password = secret\n", encoding="utf-8")
    (root / config_name).symlink_to(outside)

    assert scan_config_refs(root, {"private.module"}) == {}


@skip_file_symlink_unavailable
@pytest.mark.parametrize(
    ("config_name", "content"),
    [
        pytest.param(".codemapignore", "private-credential\n", id="ignore-file"),
        pytest.param("pyproject.toml", '[tool.codemap]\nexclude = ["private-credential"]\n', id="pyproject"),
    ],
)
def test_exclusions_ignore_symlinked_config(tmp_path: Path, config_name: str, content: str) -> None:
    """Exclusion metadata cannot include contents read through repository config symlinks."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "credentials"
    outside.write_text(content, encoding="utf-8")
    (root / config_name).symlink_to(outside)

    exclusions = _load_exclusions(root)

    assert exclusions.dirs == frozenset()
    assert exclusions.globs == ()
    assert exclusions.sources == {}


@skip_file_symlink_unavailable
def test_source_roots_ignore_symlinked_pyproject(tmp_path: Path) -> None:
    """An external pyproject cannot select source roots through a repository symlink."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    outside = tmp_path / "pyproject.toml"
    outside.write_text('[tool.codemap]\nsrc_roots = ["src"]\n', encoding="utf-8")
    (root / "pyproject.toml").symlink_to(outside)

    assert load_src_roots(root) == []


def test_source_roots_reject_existing_traversal_target(tmp_path: Path) -> None:
    """Lexically nested traversal cannot select an existing directory outside the project."""
    root = tmp_path / "project"
    (root / "nested").mkdir(parents=True)
    (root / "src").mkdir()
    (tmp_path / "outside").mkdir()
    (root / "pyproject.toml").write_text(
        '[tool.codemap]\nsrc_roots = ["nested/../../outside", "src"]\n', encoding="utf-8"
    )

    assert load_src_roots(root) == [root / "src"]


@skip_file_symlink_unavailable
def test_source_roots_ignore_cyclic_directory_link(tmp_path: Path) -> None:
    """An unresolvable configured root does not prevent valid roots from being scanned."""
    (tmp_path / "src").mkdir()
    (tmp_path / "loop").symlink_to("loop", target_is_directory=True)
    (tmp_path / "pyproject.toml").write_text('[tool.codemap]\nsrc_roots = ["loop", "src"]\n', encoding="utf-8")

    assert load_src_roots(tmp_path) == [tmp_path / "src"]


def test_mkdocs_skips_oversized_lines_and_preserves_coordinates(tmp_path: Path) -> None:
    """Oversized lines bypass reference matching without losing later reference line numbers."""
    markdown = tmp_path / "api.md"
    markdown.write_text("[" + "a" * 8192 + "][secret.module]\n[Public][public.module]\n", encoding="utf-8")

    assert scan_mkdocs_xrefs(markdown, tmp_path) == [
        {"role": "mkdocs", "target": "public::module", "file": "api.md", "line": 2, "source": "mkdocs"}
    ]


def test_mkdocs_named_regex_does_not_cross_line_boundaries() -> None:
    """Named references stay on one line even when the regex is used independently."""
    assert _MKDOCS_NAMED_RE.search("[label\ncontinued][private.module]") is None
