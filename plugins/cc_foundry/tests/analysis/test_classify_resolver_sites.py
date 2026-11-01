"""Tests for classify_resolver_sites bin script.

The ``--doctest-modules`` gate already covers the pure functions (`classify_site`, `find_sites`, `textual_count`, and
`_strip_comment`). This file covers the file-walking `scan()` and the CLI `main()`, which need `tmp_path`/`capsys` I/O.
"""

from __future__ import annotations

import sys
from pathlib import Path

import classify_resolver_sites as crs
import pytest


def _write(path: Path, text: str) -> Path:
    """Write fixture text and return its path.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     _write(Path(directory) / "site.md", "body").read_text() == "body"
        True
    """
    path.write_text(text, encoding="utf-8")
    return path


#: One extractable site: the variable is consumed by a later ``cat``.
_EXTRACTABLE_BLOCK = '```bash\n_FS=$(python resolve_shared_path.py)\ncat "$_FS/x.md"\n```\n'
#: One non-extractable site: the variable is only echoed.
_NON_EXTRACTABLE_BLOCK = '```bash\n_SUB=$(python resolve_skill_subdir.py)\necho "$_SUB"\n```\n'


class TestScan:
    """File-walking ``scan()`` over a directory tree of Markdown files."""

    def test_finds_extractable_and_non_extractable_sites(self, tmp_path: Path) -> None:
        """Each resolver call site is counted textually and classified by whether its variable is reused.

        Two files each hold one resolver call; only the one feeding a later ``cat`` can be extracted.
        """
        _write(tmp_path / "a.md", _EXTRACTABLE_BLOCK)
        _write(tmp_path / "b.md", _NON_EXTRACTABLE_BLOCK)
        textual, sites = crs.scan(tmp_path)
        assert textual["resolve_shared_path.py"] == 1
        assert textual["resolve_skill_subdir.py"] == 1
        assert {s.script: s.extractable for s in sites} == {
            "resolve_shared_path.py": True,
            "resolve_skill_subdir.py": False,
        }

    def test_empty_tree_returns_zero_counts(self, tmp_path: Path) -> None:
        """A tree with no Markdown yields zero counts for both scripts and no sites."""
        textual, sites = crs.scan(tmp_path)
        assert (textual, sites) == ({"resolve_shared_path.py": 0, "resolve_skill_subdir.py": 0}, [])

    def test_prose_mention_counts_textual_but_yields_no_site(self, tmp_path: Path) -> None:
        """A script name in prose raises the textual count but is not a call site, so it is never extractable."""
        _write(tmp_path / "prose.md", "See `resolve_shared_path.py` for details.\n")
        textual, sites = crs.scan(tmp_path)
        assert (textual["resolve_shared_path.py"], sites) == (1, [])


class TestMain:
    """The command line entry point, end to end."""

    def test_reports_counts_for_scanned_tree(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default report lists textual, site and extractable counts per script.

        The tree holds one extractable site, so ``resolve_shared_path.py`` reports 1/1/1 and the other script 0/0/0.
        """
        _write(tmp_path / "skill.md", _EXTRACTABLE_BLOCK)
        monkeypatch.setattr(sys, "argv", ["classify_resolver_sites.py", str(tmp_path)])
        crs.main()
        out = capsys.readouterr().out
        assert "resolve_shared_path.py: textual=1 invocation_sites=1 extractable=1" in out
        assert "resolve_skill_subdir.py: textual=0 invocation_sites=0 extractable=0" in out

    def test_list_extractable_prints_file_and_var(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--list-extractable`` names each extractable site's file and the variable holding the resolved path."""
        _write(tmp_path / "skill.md", _EXTRACTABLE_BLOCK)
        monkeypatch.setattr(sys, "argv", ["classify_resolver_sites.py", str(tmp_path), "--list-extractable"])
        crs.main()
        out = capsys.readouterr().out
        assert "_FS" in out
        assert str(tmp_path / "skill.md") in out
