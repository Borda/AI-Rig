"""Tests for ``research/bin/resolve_shared.py``.

Output contract:

* Cache hit → newest non-orphaned ``research/<version>/skills/_shared``.
* No cache → source-tree fallback ``plugins/cc_research/skills/_shared``
  with stderr warning. Always exits 0.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import resolve_shared

SCRIPT = Path(resolve_shared.__file__)


def test_cache_hit_returns_newest_version(tmp_path: Path) -> None:
    """Newest cached research version's ``_shared`` is returned."""
    base = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "research"
    (base / "0.1.0" / "skills" / "_shared").mkdir(parents=True)
    newer = base / "0.5.2" / "skills" / "_shared"
    newer.mkdir(parents=True)
    path, from_cache = resolve_shared.resolve(home=tmp_path)
    assert from_cache is True
    assert Path(path) == newer


@pytest.mark.parametrize(
    "older_version,newer_version",
    [pytest.param("0.9.0", "0.10.0", id="0.9.0"), pytest.param("0.20.0", "1.0.0", id="0.20.0")],
)
def test_cache_hit_uses_semver_ordering(tmp_path: Path, older_version: str, newer_version: str) -> None:
    """Newest cached research version is selected semantically, not lexicographically."""
    base = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "research"
    (base / older_version / "skills" / "_shared").mkdir(parents=True)
    newer = base / newer_version / "skills" / "_shared"
    newer.mkdir(parents=True)
    path, from_cache = resolve_shared.resolve(home=tmp_path)
    assert from_cache is True
    assert Path(path) == newer


def test_orphaned_version_skipped(tmp_path: Path) -> None:
    """Skip an orphaned newest version in favor of the next candidate."""
    base = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "research"
    orphaned = base / "0.20.0"
    (orphaned / "skills" / "_shared").mkdir(parents=True)
    (orphaned / ".orphaned_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
    older = base / "0.5.2" / "skills" / "_shared"
    older.mkdir(parents=True)
    path, from_cache = resolve_shared.resolve(home=tmp_path)
    assert from_cache is True
    assert Path(path) == older


def test_source_tree_fallback(tmp_path: Path) -> None:
    """Empty cache → source-tree fallback string, ``from_cache=False``."""
    path, from_cache = resolve_shared.resolve(home=tmp_path)
    assert from_cache is False
    assert path == "plugins/cc_research/skills/_shared"


def test_main_cache_hit_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report only the resolved path for a cache hit."""
    cache = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "research" / "0.5.2" / "skills" / "_shared"
    cache.mkdir(parents=True)
    monkeypatch.setattr(resolve_shared.Path, "home", classmethod(lambda _cls: tmp_path))
    rc = resolve_shared.main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == str(cache)
    assert captured.err == ""


def test_main_fallback_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Warn while returning a successful fallback without a cache."""
    monkeypatch.setattr(resolve_shared.Path, "home", classmethod(lambda _cls: tmp_path))
    rc = resolve_shared.main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "plugins/cc_research/skills/_shared"
    assert "source-tree fallback" in captured.err
