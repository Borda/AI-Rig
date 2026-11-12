"""Tests for ``develop/bin/dev_shared_resolve.py``.

Output contract:

* One stdout line: this plugin's own ``skills/_shared`` path.
* ``--init``: persists ``dev-shared`` and a fresh ``dev-agent-watch-dir`` sentinel instead, stdout empty.
* Cache hit returns newest semver; orphaned versions are skipped.
* Absent cache → source-tree fallback ``plugins/cc_develop/skills/_shared``, printed only when the working directory is
  the develop source repository (``plugins/cc_develop/.claude-plugin/plugin.json`` naming ``develop``); otherwise the
  CLI exits 1 without printing or persisting anything.

The former ``--foundry`` flag (which emitted a sibling plugin's ``_shared`` on a
second line) was removed — see ``plugins/CLAUDE.md`` §Self-Contained ``_shared``.
``TestNoSiblingReachIn`` below guards against its reintroduction.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import dev_shared_resolve
import pytest
from dev_run_dir import read_sentinel

_RESOLVED = "/resolved/_shared"


def _write_file(root: Path, relpath: str, text: str) -> None:
    """Create ``root/relpath`` with ``text``, making parent directories as needed."""
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _install(home: Path, version: str) -> Path:
    """Create a cached develop install at ``version`` under ``home`` and return its ``skills/_shared`` directory."""
    shared = home / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "develop" / version / "skills" / "_shared"
    shared.mkdir(parents=True)
    root = shared.parent.parent
    _write_file(root, ".claude-plugin/plugin.json", '{"name":"develop"}')
    _write_file(root, "bin/resolve_plugin_root.py", "# resolver\n")
    return shared


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at an empty directory so no real plugin cache can answer."""
    home = tmp_path / "home"
    monkeypatch.setattr(dev_shared_resolve.Path, "home", classmethod(lambda _cls: home))
    return home


@pytest.fixture
def source_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_home: Path) -> Path:
    """Enter a working directory that is the develop source repository, with an empty plugin cache."""
    repo = tmp_path / "repo"
    _write_file(repo, "plugins/cc_develop/.claude-plugin/plugin.json", '{"name": "develop"}')
    _write_file(repo, "plugins/cc_develop/bin/resolve_plugin_root.py", "# resolver\n")
    monkeypatch.setattr(dev_shared_resolve, "__file__", str(repo / "plugins/cc_develop/bin/dev_shared_resolve.py"))
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def foreign_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_home: Path) -> Path:
    """Enter a working directory carrying its own ``plugins/cc_develop/skills/_shared`` but no develop manifest."""
    (tmp_path / "plugins" / "cc_develop" / "skills" / "_shared").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestDevelopOnly:
    """Resolution always targets develop's own shared dir."""

    @pytest.mark.parametrize(
        ("older_version", "newer_version"),
        [
            pytest.param("0.1.0", "0.6.2", id="0.1.0"),
            pytest.param("0.9.0", "0.10.0", id="0.9.0"),
            pytest.param("0.99.0", "1.0.0", id="0.99.0"),
        ],
    )
    def test_cache_hit_uses_semver_ordering(self, tmp_path: Path, older_version: str, newer_version: str) -> None:
        """Newest cached develop version's ``_shared`` is selected semantically, not lexicographically.

        A plain newer version wins, and so does one that sorts lower as text (0.10.0 over 0.9.0, 1.0.0 over 0.99.0).
        """
        _install(tmp_path, older_version)
        newer = _install(tmp_path, newer_version)
        assert Path(dev_shared_resolve.resolve_shared_path(home=tmp_path)) == newer

    def test_registry_beats_newer_cached_version(self, tmp_path: Path) -> None:
        """Shared documents follow the active record rather than a newer cached version."""
        active = _install(tmp_path, "0.1.0")
        _install(tmp_path, "0.6.2")
        registry = tmp_path / ".claude/plugins/installed_plugins.json"
        registry.write_text(
            json.dumps(
                {
                    "plugins": {
                        "develop@borda-ai-rig": [
                            {"installPath": str(active.parent.parent), "installedAt": "2026-01-01T00:00:00Z"},
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        assert Path(dev_shared_resolve.resolve_shared_path(home=tmp_path)) == active

    def test_skips_orphaned_newest_version(self, tmp_path: Path) -> None:
        """Skip an orphaned newest version in favor of the previous release.

        Claude Code marks a replaced version with ``.orphaned_at`` and leaves it on disk; resolving into it would load
        protocol files from a plugin version that is no longer installed.
        """
        orphaned = _install(tmp_path, "0.20.0")
        (orphaned.parent.parent / ".orphaned_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
        older = _install(tmp_path, "0.6.2")
        assert Path(dev_shared_resolve.resolve_shared_path(home=tmp_path)) == older

    def test_falls_back_to_source_tree_path(self, tmp_path: Path) -> None:
        """An empty cache resolves to the source-tree fallback path string."""
        assert dev_shared_resolve.resolve_shared_path(home=tmp_path) == "plugins/cc_develop/skills/_shared"

    def test_source_fallback_warns_on_stderr(self, source_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Without an installed plugin the working-directory path is announced on stderr, never taken silently.

        Even inside the develop source repository the fallback tier is local-dev only, so the caller must be able to see
        that it was used.
        """
        dev_shared_resolve.main([])

        assert "working directory" in capsys.readouterr().err

    def test_cache_hit_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An installed plugin resolves without any stderr noise."""
        monkeypatch.setattr(dev_shared_resolve.Path, "home", classmethod(lambda _cls: tmp_path))
        _install(tmp_path, "0.1.0")

        dev_shared_resolve.main([])

        assert capsys.readouterr().err == ""

    def test_prints_single_line(self, source_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Emit exactly one output line, the plugin's own path, when no option is provided.

        The output contract is a single path — a second line would be a sibling leak.
        """
        rc = dev_shared_resolve.main([])
        lines = [line for line in capsys.readouterr().out.splitlines() if line]
        assert (rc, lines) == (0, ["plugins/cc_develop/skills/_shared"])


class TestSourceRepoGate:
    """The working-directory tier is trusted only inside the develop plugin's own source repository."""

    @pytest.mark.parametrize(
        ("relpath", "text"),
        [
            pytest.param("plugins/cc_develop/README.md", "readme", id="no-manifest"),
            pytest.param(
                "plugins/cc_develop/.claude-plugin/plugin.json",
                '{"name": "research"}',
                id="manifest-names-other-plugin",
            ),
            pytest.param("plugins/cc_develop/.claude-plugin/plugin.json", "{not json", id="malformed-manifest"),
            pytest.param("plugins/cc_develop/.claude-plugin/plugin.json", '["develop"]', id="non-object-manifest"),
        ],
    )
    def test_untrusted_cwd_exits_nonzero(
        self, foreign_tree: Path, capsys: pytest.CaptureFixture[str], relpath: str, text: str
    ) -> None:
        """With no install, a cwd lacking develop's own manifest is refused instead of printed.

        A repository under analysis can carry its own ``plugins/cc_develop/skills/_shared``. Skills ``cat`` protocol
        files from the printed directory, so printing it would hand that repository's text to the agent as protocol.
        """
        _write_file(foreign_tree, relpath, text)

        rc = dev_shared_resolve.main([])

        captured = capsys.readouterr()
        assert (rc, captured.out, "not the develop plugin source repository" in captured.err) == (1, "", True)

    def test_untrusted_cwd_init_writes_no_sentinel(self, foreign_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--init`` refuses an untrusted cwd before persisting, so no later warm read picks up its tree.

        Every later develop block warm-reads ``dev-shared``; a sentinel written here would carry the refused path past
        the gate.
        """
        monkeypatch.setenv("TMPDIR", str(foreign_tree))
        monkeypatch.setenv("CSID", "gate-test")

        rc = dev_shared_resolve.main(["--init"])

        assert (rc, read_sentinel("dev-shared")) == (1, "")


class TestNoSiblingReachIn:
    """The resolver must never expose another plugin's tree."""

    def test_rejects_retired_foundry_flag(self) -> None:
        """The retired ``--foundry`` option is rejected so a sibling plugin's ``_shared`` can never be emitted."""
        with pytest.raises(SystemExit):
            dev_shared_resolve.main(["--foundry"])


class TestInit:
    """``--init`` persists the cold-start sentinels every develop skill's later blocks read."""

    @pytest.fixture(autouse=True)
    def _isolate_sentinels(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Point ``$TMPDIR`` at an empty directory and fix the session token, so only this test's sentinels exist."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "init-test")
        monkeypatch.chdir(tmp_path)

    @mock.patch("dev_shared_resolve._locate", autospec=True, return_value=(_RESOLVED, True))
    def test_writes_shared_path_and_prints_nothing(self, mock_locate, capsys: pytest.CaptureFixture[str]) -> None:
        """``--init`` stores the resolved path under ``dev-shared`` and leaves stdout empty.

        A skill used to capture the path with ``$(python …)`` and echo it into the sentinel itself; now one call does
        both, and the warm read that follows is the only consumer of the value.
        """
        rc = dev_shared_resolve.main(["--init"])

        assert (rc, capsys.readouterr().out, read_sentinel("dev-shared")) == (0, "", _RESOLVED)

    @mock.patch("dev_shared_resolve._locate", autospec=True, return_value=(_RESOLVED, True))
    def test_without_init_prints_path_and_writes_no_sentinel(
        self, mock_locate, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The default mode keeps its stdout contract and stays free of side effects."""
        rc = dev_shared_resolve.main([])

        assert (rc, capsys.readouterr().out, read_sentinel("dev-shared"), read_sentinel("dev-agent-watch-dir")) == (
            0,
            f"{_RESOLVED}\n",
            "",
            "",
        )

    def test_watch_dir_is_absolute_and_fresh_per_run(self, tmp_path: Path) -> None:
        """Each run gets its own timestamped watch dir under the working directory.

        A prior run's deadline batches live in the previous directory, so they never report as stale in this run
        (``agent-resolution.md`` §Agent waits).
        """
        first = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        later = datetime(2026, 1, 2, 3, 9, 9, tzinfo=timezone.utc)

        assert dev_shared_resolve.persist_run_sentinels("/s", cwd=tmp_path, now=first)
        first_dir = read_sentinel("dev-agent-watch-dir")
        assert dev_shared_resolve.persist_run_sentinels("/s", cwd=tmp_path, now=later)
        later_dir = read_sentinel("dev-agent-watch-dir")

        assert first_dir == f"{tmp_path.as_posix()}/.temp/develop/agent-watch-2026-01-02T03-04-05Z"
        assert later_dir == f"{tmp_path.as_posix()}/.temp/develop/agent-watch-2026-01-02T03-09-09Z"

    @mock.patch("dev_shared_resolve._locate", autospec=True, return_value=(_RESOLVED, True))
    def test_unwritable_sentinel_dir_warns_and_still_exits_zero(
        self, mock_locate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A sentinel write failure never fails the skill start; the skill's warm-read fallback takes over."""
        monkeypatch.setenv("TMPDIR", str(tmp_path / "missing" / "dir"))

        rc = dev_shared_resolve.main(["--init"])

        assert (rc, "could not write the session sentinels" in capsys.readouterr().err) == (0, True)
