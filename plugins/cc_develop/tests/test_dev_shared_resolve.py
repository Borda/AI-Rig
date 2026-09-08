"""Tests for ``develop/bin/dev_shared_resolve.py``.

Output contract:

* One stdout line: this plugin's own ``skills/_shared`` path.
* Cache hit returns newest semver; orphaned versions are skipped.
* Absent cache → source-tree fallback ``plugins/cc_develop/skills/_shared``.

The former ``--foundry`` flag (which emitted a sibling plugin's ``_shared`` on a
second line) was removed — see ``plugins/CLAUDE.md`` §Self-Contained ``_shared``.
``TestNoSiblingReachIn`` below guards against its reintroduction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import dev_shared_resolve


class TestDevelopOnly:
    """Resolution always targets develop's own shared dir."""

    def test_cache_hit_returns_newest_version(self, tmp_path: Path) -> None:
        """Newest cached develop version's ``_shared`` is returned."""
        base = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "develop"
        (base / "0.1.0" / "skills" / "_shared").mkdir(parents=True)
        newer = base / "0.6.2" / "skills" / "_shared"
        newer.mkdir(parents=True)
        assert dev_shared_resolve.resolve_shared_path(home=tmp_path) == str(newer)

    @pytest.mark.parametrize(
        "older_version,newer_version",
        [pytest.param("0.9.0", "0.10.0", id="0.9.0"), pytest.param("0.99.0", "1.0.0", id="0.99.0")],
    )
    def test_cache_hit_uses_semver_ordering(self, tmp_path: Path, older_version: str, newer_version: str) -> None:
        """Newest cached develop version is selected semantically, not lexicographically."""
        base = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "develop"
        (base / older_version / "skills" / "_shared").mkdir(parents=True)
        newer = base / newer_version / "skills" / "_shared"
        newer.mkdir(parents=True)
        assert dev_shared_resolve.resolve_shared_path(home=tmp_path) == str(newer)

    def test_orphaned_develop_version_skipped(self, tmp_path: Path) -> None:
        """Skip an orphaned newest version in favor of the previous release."""
        base = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "develop"
        orphaned = base / "0.20.0"
        (orphaned / "skills" / "_shared").mkdir(parents=True)
        (orphaned / ".orphaned_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
        older = base / "0.6.2" / "skills" / "_shared"
        older.mkdir(parents=True)
        assert dev_shared_resolve.resolve_shared_path(home=tmp_path) == str(older)

    def test_source_fallback(self, tmp_path: Path) -> None:
        """Empty cache → source-tree fallback path string."""
        assert dev_shared_resolve.resolve_shared_path(home=tmp_path) == "plugins/cc_develop/skills/_shared"

    def test_main_prints_single_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Emit one output line when no option is provided."""
        monkeypatch.setattr(dev_shared_resolve.Path, "home", classmethod(lambda _cls: tmp_path))
        rc = dev_shared_resolve.main([])
        captured = capsys.readouterr()
        assert rc == 0
        lines = [line for line in captured.out.splitlines() if line]
        assert lines == ["plugins/cc_develop/skills/_shared"]


class TestNoSiblingReachIn:
    """The resolver must never expose another plugin's tree."""

    def test_foundry_flag_is_rejected(self) -> None:
        """Reject the retired Foundry option."""
        with pytest.raises(SystemExit):
            dev_shared_resolve.main(["--foundry"])

    def test_main_prints_exactly_one_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Output contract is a single path — a second line would be a sibling leak."""
        monkeypatch.setattr(dev_shared_resolve.Path, "home", classmethod(lambda _cls: tmp_path))
        rc = dev_shared_resolve.main([])
        lines = [line for line in capsys.readouterr().out.splitlines() if line]
        assert rc == 0
        assert len(lines) == 1
