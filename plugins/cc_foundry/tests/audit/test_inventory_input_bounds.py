"""Exercise inventory readers with malformed Markdown and confined input paths."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import consolidate_inventory as ci
import pytest
from _hook_env import _symlinks_supported


@pytest.mark.parametrize(
    ("operation", "text", "expected"),
    [
        pytest.param(
            "_plain_title", "a" + "_" * 100_000 + "b", "a" + "_" * 100_000 + "b", id="internal-underscore-run"
        ),
        pytest.param("_plain_title", "<!--" * 25_000, "<!--" * 25_000, id="unfinished-comments"),
        pytest.param("_plain_title", "<?" * 50_000, "<?" * 50_000, id="unfinished-processing-instructions"),
        pytest.param("_plain_title", "<![CDATA[" * 12_000, "<CDATA" * 12_000, id="unfinished-cdata"),
        pytest.param("_plain_title", "<!A" * 34_000, "<!A" * 34_000, id="unfinished-declarations"),
        pytest.param("_drop_link_targets", "](" * 25_000, "](" * 25_000, id="unfinished-link-targets"),
        pytest.param("_TYPED_PLAIN.match", "1" * 100_000 + "x", False, id="non-numeric-long-scalar"),
        pytest.param("_CITATION.findall", "a" * 100_000, [], id="non-citation-long-token"),
        pytest.param("_CITATION.findall", "f.md §" + "a" * 100_000, True, id="long-cited-title"),
        pytest.param("_CITATION.findall", "f.md " + " " * 100_000, [], id="unfinished-citation-spacing"),
        pytest.param("_block_dependencies", "<!-- policy-sibling:" + " " * 100_000, None, id="unfinished-sibling"),
    ],
)
def test_markdown_parsing_finishes_without_quadratic_retry(operation: str, text: str, expected: object) -> None:
    """A 100 KB malformed token cannot hold the deterministic reader for several seconds."""
    script = (
        "import json,sys; from operator import attrgetter; from pathlib import Path; import consolidate_inventory as ci; "
        "op,text,want=json.load(sys.stdin); "
        "result=ci._block_dependencies([], [], text, Path('.')) if op=='_block_dependencies' else "
        "attrgetter(op)(ci)(text); "
        "result=bool(result) if isinstance(want,bool) else result; "
        "assert result==want, repr(result)[:100]"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps([operation, text, expected]),
        cwd=Path(ci.__file__).parent,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("text", "plain"),
    [
        pytest.param('<span title="<!--">Visible-->', "Visible-->", id="comment-opener-inside-tag"),
        pytest.param("[label](<one\\\ntwo>)", "label(<one\\\ntwo>)", id="escaped-newline-is-not-angle-target"),
        pytest.param("[label](a](b))", "label", id="bracket-inside-balanced-destination"),
        pytest.param("[label](<one two>)", "label", id="spaces-inside-angle-target"),
        pytest.param("[label](a\\ b)", "label", id="escaped-space-in-destination"),
        pytest.param("[label](unclosed[label](valid)", "label(unclosedlabel", id="valid-suffix-after-invalid-target"),
    ],
)
def test_title_parser_retains_markdown_destination_semantics(text: str, plain: str) -> None:
    """Faster scanning preserves tag, escape, and balanced destination behavior."""
    assert ci._plain_title(text) == plain


def test_markdown_directory_is_not_hashed(tmp_path: Path) -> None:
    """A directory ending in .md is ignored by both the digest and citation reader."""
    root = tmp_path / "repo"
    (root / "plugins/example/empty.md").mkdir(parents=True)
    assert (ci._markdown_files(root), ci._mentions(root)) == ([], [])
    assert ci.citation_digest(root) == ci.hashlib.sha256(b"").hexdigest()


@pytest.mark.skipif(not _symlinks_supported(), reason="symlink creation is unavailable")
@pytest.mark.parametrize("dangling", [False, True])
def test_markdown_links_outside_root_are_not_read(tmp_path: Path, dangling: bool) -> None:
    """External and dangling Markdown symlinks cannot affect the digest or cited headings."""
    root = tmp_path / "repo"
    plugin = root / "plugins/example"
    plugin.mkdir(parents=True)
    outside = tmp_path / "external.md"
    outside.write_text("see rules.md §External\n", encoding="utf-8")
    (plugin / "external.md").symlink_to(outside.with_name("absent.md") if dangling else outside)
    assert (ci._markdown_files(root), ci._mentions(root)) == ([], [])
    assert ci.citation_digest(root) == ci.hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("pattern", ["../outside.json", "sub/*.json", "sub\\*.json", "..json"])
def test_collect_refuses_patterns_with_path_components(tmp_path: Path, pattern: str) -> None:
    """Collection accepts only a bare filename pattern and leaves the output absent on refusal."""
    folder = tmp_path / ".temp/run"
    folder.mkdir(parents=True)
    (folder.parent / "outside.json").write_text('[{"private": true}]', encoding="utf-8")
    out = folder / "joined.json"
    code = ci.main(["collect", "--root", str(tmp_path), "--dir", str(folder), "--glob", pattern, "--out", str(out)])
    assert (code, out.exists()) == (1, False)
