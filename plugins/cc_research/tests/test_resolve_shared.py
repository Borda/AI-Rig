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


@pytest.mark.parametrize(
    ("older_version", "newer_version"),
    [
        pytest.param("0.1.0", "0.5.2", id="plain-newest-wins"),
        pytest.param("0.9.0", "0.10.0", id="semver-minor-0.9-vs-0.10"),
        pytest.param("0.20.0", "1.0.0", id="semver-major-0.20-vs-1.0"),
    ],
)
def test_cache_hit_returns_newest_version(tmp_path: Path, older_version: str, newer_version: str) -> None:
    """Newest cached research version's ``_shared`` is returned, selected semantically, not lexicographically.

    A plain newest-wins pair, a pair where lexicographic ordering would pick the wrong winner (``0.9.0`` sorts after
    ``0.10.0`` as text), and a pair spanning a major bump (``0.20.0`` vs ``1.0.0``) show versions are compared as
    numbers.
    """
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
