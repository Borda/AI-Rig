"""Preserve lexical scan coordinates and bound MkDocs named-link matching."""

from pathlib import Path

from _host_skips import skip_file_symlink_unavailable
from _scan_helpers import IGNORE_RUNNER_THREAD_FORK_WARNING, write_tree
from codemap_py import graph
from codemap_py.scanner.docs_xrefs import _MKDOCS_NAMED_RE, scan_mkdocs_xrefs


@IGNORE_RUNNER_THREAD_FORK_WARNING
def test_relative_configured_root_scans(tmp_path, monkeypatch):
    """A relative scan root and its configured source tree share lexical coordinates."""
    write_tree(
        tmp_path,
        {
            "project/pyproject.toml": '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
            "project/src/pkg/__init__.py": "VALUE = 1\n",
        },
    )
    monkeypatch.chdir(tmp_path)
    result = graph.scan(Path("project"))
    assert [(m["name"], m["path"]) for m in result["modules"]] == [("pkg", "src/pkg/__init__.py")]


@skip_file_symlink_unavailable
@IGNORE_RUNNER_THREAD_FORK_WARNING
def test_symlinked_configured_root_scans(tmp_path):
    """A root reached through a symlink retains matching walk and source-root coordinates."""
    write_tree(
        tmp_path,
        {
            "project/pyproject.toml": '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
            "project/src/pkg/__init__.py": "VALUE = 1\n",
        },
    )
    link = tmp_path / "linked"
    link.symlink_to(tmp_path / "project", target_is_directory=True)
    result = graph.scan(link)
    assert [(m["name"], m["path"]) for m in result["modules"]] == [("pkg", "src/pkg/__init__.py")]


def test_named_link_stops_at_next_opener():
    """A failed opener cannot consume later openers while seeking a closing label."""
    match = _MKDOCS_NAMED_RE.search("[[[label][pkg.fn]")
    assert match is not None
    assert match.start() == 2


def test_nested_bracket_labels_keep_existing_xrefs(tmp_path):
    """Balanced nested labels stay unsupported while an inner complete label still resolves."""
    markdown = tmp_path / "api.md"
    markdown.write_text("[outer [inner]][pkg.fn]\n[outer [inner][pkg.other]\n[plain][pkg.plain]\n")
    assert [entry["target"] for entry in scan_mkdocs_xrefs(markdown, tmp_path)] == ["pkg::other", "pkg::plain"]
