"""Tests for ``research/bin/resolve_shared.py``.

Output contract:

* Cache hit → newest non-orphaned ``research/<version>/skills/_shared``.
* No cache → source-tree fallback ``plugins/cc_research/skills/_shared``
  with stderr warning, only when the working directory is the research source repository
  (``plugins/cc_research/.claude-plugin/plugin.json`` naming ``research``); otherwise exit 1.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import resolve_shared

SCRIPT = Path(resolve_shared.__file__)
_CACHE = Path(".claude/plugins/cache/borda-ai-rig/research")


def _write_file(root: Path, relpath: str, text: str) -> None:
    """Create ``root/relpath`` with ``text``, making parent directories as needed."""
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at ``tmp_path`` so the real plugin cache is never consulted."""
    monkeypatch.setattr(resolve_shared.Path, "home", classmethod(lambda _cls: tmp_path))
    return tmp_path


class TestResolve:
    """``resolve()`` choosing between cached versions and the source-tree fallback."""

    @pytest.mark.parametrize(
        ("older_version", "newer_version"),
        [
            pytest.param("0.1.0", "0.5.2", id="plain-newest-wins"),
            pytest.param("0.9.0", "0.10.0", id="semver-minor-0.9-vs-0.10"),
            pytest.param("0.20.0", "1.0.0", id="semver-major-0.20-vs-1.0"),
        ],
    )
    def test_cache_hit_returns_newest_version(self, tmp_path: Path, older_version: str, newer_version: str) -> None:
        """Newest cached research version's ``_shared`` is returned, selected semantically, not lexicographically.

        A plain newest-wins pair, a pair where lexicographic ordering would pick the wrong winner (``0.9.0`` sorts after
        ``0.10.0`` as text), and a pair spanning a major bump (``0.20.0`` vs ``1.0.0``) show versions are compared as
        numbers.
        """
        base = tmp_path / _CACHE
        (base / older_version / "skills" / "_shared").mkdir(parents=True)
        newer = base / newer_version / "skills" / "_shared"
        newer.mkdir(parents=True)

        path, from_cache = resolve_shared.resolve(home=tmp_path)

        assert from_cache is True
        assert Path(path) == newer

    def test_skips_orphaned_version(self, tmp_path: Path) -> None:
        """An orphaned newest version is skipped in favour of the next candidate.

        After an upgrade the superseded install keeps its directory but carries ``.orphaned_at``; it must never shadow
        the live install even though its version number is higher.
        """
        base = tmp_path / _CACHE
        orphaned = base / "0.20.0"
        (orphaned / "skills" / "_shared").mkdir(parents=True)
        (orphaned / ".orphaned_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
        older = base / "0.5.2" / "skills" / "_shared"
        older.mkdir(parents=True)

        path, from_cache = resolve_shared.resolve(home=tmp_path)

        assert from_cache is True
        assert Path(path) == older

    def test_empty_cache_falls_back_to_source_tree(self, tmp_path: Path) -> None:
        """Empty cache → source-tree fallback string, ``from_cache=False``."""
        path, from_cache = resolve_shared.resolve(home=tmp_path)

        assert from_cache is False
        assert path == "plugins/cc_research/skills/_shared"


class TestMain:
    """``main()`` output and exit status for the plain (no ``--persist``/``--emit``) call."""

    def test_cache_hit_prints_path_without_warning(self, fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A cache hit prints only the resolved path and leaves stderr empty."""
        cache = fake_home / _CACHE / "0.5.2" / "skills" / "_shared"
        cache.mkdir(parents=True)

        rc = resolve_shared.main([])

        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == str(cache)
        assert captured.err == ""

    def test_fallback_in_source_repo_warns(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Inside the research source repository, warn while returning a successful fallback without a cache."""
        _write_file(tmp_path, "plugins/cc_research/.claude-plugin/plugin.json", '{"name": "research"}')
        monkeypatch.chdir(tmp_path)

        rc = resolve_shared.main([])

        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "plugins/cc_research/skills/_shared"
        assert "source-tree fallback" in captured.err

    @pytest.mark.parametrize(
        ("relpath", "text"),
        [
            pytest.param("plugins/cc_research/README.md", "readme", id="no-manifest"),
            pytest.param(
                "plugins/cc_research/.claude-plugin/plugin.json",
                '{"name": "develop"}',
                id="manifest-names-other-plugin",
            ),
            pytest.param("plugins/cc_research/.claude-plugin/plugin.json", "{not json", id="malformed-manifest"),
        ],
    )
    def test_untrusted_cwd_exits_nonzero(
        self,
        tmp_path: Path,
        fake_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        relpath: str,
        text: str,
    ) -> None:
        """With no install, a cwd lacking research's own manifest is refused even without flags.

        A repository under analysis can carry its own ``plugins/cc_research/skills/_shared``; printing it would let a
        caller ``cat`` that repository's text as protocol.
        """
        (tmp_path / "plugins" / "cc_research" / "skills" / "_shared").mkdir(parents=True)
        _write_file(tmp_path, relpath, text)
        monkeypatch.chdir(tmp_path)

        rc = resolve_shared.main([])

        captured = capsys.readouterr()
        assert rc == 1
        assert captured.out == ""
        assert "not the research plugin source repository" in captured.err

    def test_unresolvable_shared_dir_fails_closed(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No cache and no source tree under the cwd → exit 1 with the resolution-failed message, nothing persisted."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess1")

        rc = resolve_shared.main(["--persist", "research-shared", "--emit", "agent-resolution.md"])

        assert rc == 1
        assert "! Plugin path resolution failed" in capsys.readouterr().err
        assert not (tmp_path / "research-shared-sess1").exists()


class TestPersistAndEmit:
    """``--persist`` sentinel writes and ``--emit`` document output against a cached ``_shared`` dir."""

    @pytest.fixture
    def cached_shared(self, fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Build a cached research ``_shared`` dir with two docs; pin ``TMPDIR`` and ``CSID`` under ``tmp_path``."""
        shared = fake_home / _CACHE / "0.5.2" / "skills" / "_shared"
        shared.mkdir(parents=True)
        (shared / "agent-resolution.md").write_text("# agents\n", encoding="utf-8")
        (shared / "unsupported-flag-protocol.md").write_text("# flags\n", encoding="utf-8")
        tmp = fake_home / "tmp"
        tmp.mkdir()
        monkeypatch.setenv("TMPDIR", str(tmp))
        monkeypatch.setenv("CSID", "sess1")
        return shared

    def test_persist_writes_session_scoped_sentinel(
        self, cached_shared: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--persist`` writes ``<tmp>/<slug>-<CSID>`` (resolved path, trailing newline) and still prints the path."""
        rc = resolve_shared.main(["--persist", "research-shared"])

        sentinel = Path(os.environ["TMPDIR"]) / "research-shared-sess1"
        assert rc == 0
        assert sentinel.read_text(encoding="utf-8") == f"{cached_shared}\n"
        assert capsys.readouterr().out.strip() == str(cached_shared)

    def test_emit_prints_docs_in_order_without_path(
        self, cached_shared: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--emit`` prints each document in argument order and does not print the path itself."""
        rc = resolve_shared.main(["--emit", "unsupported-flag-protocol.md", "--emit", "agent-resolution.md"])

        assert rc == 0
        assert capsys.readouterr().out == "# flags\n# agents\n"

    def test_persist_and_emit_together(self, cached_shared: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The Agent Resolution call shape persists the sentinel and emits the doc in one run."""
        rc = resolve_shared.main(["--persist", "research-shared", "--emit", "agent-resolution.md"])

        assert rc == 0
        assert capsys.readouterr().out == "# agents\n"
        assert (Path(os.environ["TMPDIR"]) / "research-shared-sess1").is_file()

    def test_missing_doc_fails_with_bang_message(self, cached_shared: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A document absent from ``_shared`` exits 1 with a ``!`` message instead of printing nothing."""
        rc = resolve_shared.main(["--emit", "no-such-doc.md"])

        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err.startswith("! no-such-doc.md not found under")
        assert captured.out == ""

    @pytest.mark.parametrize("doc", ["../secret.md", "sub/doc.md", "..\\secret.md", ".."])
    def test_emit_rejects_non_basename(self, cached_shared: Path, doc: str, capsys: pytest.CaptureFixture[str]) -> None:
        """``--emit`` refuses anything but a bare file name, so it cannot read outside ``_shared``.

        Traversal segments, subdirectories and backslash variants all exit 2 with nothing printed.
        """
        assert resolve_shared.main(["--emit", doc]) == 2
        assert capsys.readouterr().out == ""
