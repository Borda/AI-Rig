"""Tests for the ``CODEMAP_COORDINATION_DIR`` override.

The override exists for deployments whose index directory cannot hold lock state: a read-only or shared index mount, or
a sandbox that must grant write access to the gate without granting it beside the index. The contract has two halves
that must never drift apart — the directory the path resolver reports is the directory the gate actually initialises,
and with the override set nothing is created beside the index at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BIN = str(Path(__file__).resolve().parent.parent.parent / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import _rwgate  # noqa: E402  (path set above)

from codemap_py import index_paths  # noqa: E402  (the shim above puts src/ on sys.path)


@pytest.fixture
def index(tmp_path: Path) -> Path:
    """Build a minimal JSON index in its own default-layout directory."""
    index_dir = tmp_path / "project" / ".cache" / "codemap"
    index_dir.mkdir(parents=True)
    target = index_dir / "project.json"
    target.write_text(json.dumps({"v": 1}), encoding="utf-8")
    return target


class TestCoordinationRoot:
    def test_defaults_to_the_sibling_directory(self, index: Path) -> None:
        """Without the override the coordination root stays beside the index.

        This is the shipped layout every existing deployment resolves, so a change to the override path must leave it
        untouched — the override is additive, never a redirection of the default.
        """
        assert index_paths.coordination_root(index.parent, override=None) == index.parent / ".index-rw"

    def test_reads_the_environment_override(self, index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``CODEMAP_COORDINATION_DIR`` names the coordination root itself.

        The variable is a directory, not a base holding one root per project, so the resolved path is the value as given
        rather than a path derived from it.
        """
        elsewhere = tmp_path / "gate"
        monkeypatch.setenv(index_paths.COORDINATION_DIR_ENV, str(elsewhere))

        assert index_paths.coordination_root(index.parent) == elsewhere.resolve()

    def test_moves_the_identity_without_moving_the_index(
        self, index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolved identity carries the override while its index path is unchanged.

        This separation is the whole point of the variable: ``CODEMAP_INDEX_DIR`` moves both the index and its lock
        state, whereas a read-only index mount needs only the lock state to move.
        """
        elsewhere = tmp_path / "gate"
        monkeypatch.setenv(index_paths.COORDINATION_DIR_ENV, str(elsewhere))

        identity = index_paths.resolve_index(root=index.parent.parent.parent)

        assert (identity.coordination_dir, identity.index_path) == (elsewhere.resolve(), index)


class TestGatedRead:
    def test_initialises_the_override_root(self, index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A gated read builds the lock skeleton inside the override directory.

        The gate must initialise the same directory the resolver leases; if it derived its own, readers and writers
        would coordinate through a directory nobody else consults.
        """
        elsewhere = tmp_path / "gate"
        monkeypatch.setenv(index_paths.COORDINATION_DIR_ENV, str(elsewhere))

        with _rwgate.read_index(index, timeout=5.0) as data:
            assert data == {"v": 1}

        assert {entry.name for entry in elsewhere.iterdir()} == {"readers", "registry.lock"}

    def test_leaves_the_index_directory_untouched(
        self, index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the override set, nothing is written beside the index.

        A deployment adopts the override precisely because the index directory must stay free of writes — a stray
        skeleton there would defeat the read-only mount or the sandbox rule the override was set to satisfy.
        """
        monkeypatch.setenv(index_paths.COORDINATION_DIR_ENV, str(tmp_path / "gate"))

        with _rwgate.read_index(index, timeout=5.0):
            pass

        assert {entry.name for entry in index.parent.iterdir()} == {"project.json"}
