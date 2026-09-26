"""Tests for classify_resolver_sites bin script.

The ``--doctest-modules`` gate already covers the pure functions (`classify_site`, `find_sites`, `textual_count`, and
`_strip_comment`). This file covers the file-walking `scan()` and the CLI `main()`, which need `tmp_path`/`capsys` I/O.
"""

from __future__ import annotations

import sys
from pathlib import Path

import classify_resolver_sites as crs


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


class TestScan:
    """Covers scan() walking a directory tree of .md files."""

    def test_finds_extractable_and_non_extractable_sites(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "a.md",
            '```bash\n_FS=$(python resolve_shared_path.py)\ncat "$_FS/x.md"\n```\n',
        )
        _write(
            tmp_path / "b.md",
            '```bash\n_SUB=$(python resolve_skill_subdir.py)\necho "$_SUB"\n```\n',
        )
        textual, sites = crs.scan(tmp_path)
        assert textual["resolve_shared_path.py"] == 1
        assert textual["resolve_skill_subdir.py"] == 1
        by_script = {s.script: s.extractable for s in sites}
        assert by_script["resolve_shared_path.py"] is True
        assert by_script["resolve_skill_subdir.py"] is False

    def test_empty_tree_returns_zero_counts(self, tmp_path: Path) -> None:
        textual, sites = crs.scan(tmp_path)
        assert textual == {"resolve_shared_path.py": 0, "resolve_skill_subdir.py": 0}
        assert sites == []

    def test_prose_mention_counts_textual_but_yields_no_site(self, tmp_path: Path) -> None:
        _write(tmp_path / "prose.md", "See `resolve_shared_path.py` for details.\n")
        textual, sites = crs.scan(tmp_path)
        assert textual["resolve_shared_path.py"] == 1
        assert sites == []


class TestMain:
    """Covers the CLI entry point end to end."""

    def test_reports_counts_for_scanned_tree(self, tmp_path: Path, capsys, monkeypatch) -> None:
        _write(
            tmp_path / "skill.md",
            '```bash\n_FS=$(python resolve_shared_path.py)\ncat "$_FS/x.md"\n```\n',
        )
        monkeypatch.setattr(sys, "argv", ["classify_resolver_sites.py", str(tmp_path)])
        crs.main()
        out = capsys.readouterr().out
        assert "resolve_shared_path.py: textual=1 invocation_sites=1 extractable=1" in out
        assert "resolve_skill_subdir.py: textual=0 invocation_sites=0 extractable=0" in out

    def test_list_extractable_prints_file_and_var(self, tmp_path: Path, capsys, monkeypatch) -> None:
        _write(
            tmp_path / "skill.md",
            '```bash\n_FS=$(python resolve_shared_path.py)\ncat "$_FS/x.md"\n```\n',
        )
        monkeypatch.setattr(sys, "argv", ["classify_resolver_sites.py", str(tmp_path), "--list-extractable"])
        crs.main()
        out = capsys.readouterr().out
        assert "_FS" in out
        assert str(tmp_path / "skill.md") in out
