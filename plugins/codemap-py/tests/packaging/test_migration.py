"""Rename-migration invariants for the codemap -> codemap-py identity.

Asserts the repository never carries both plugin identities at once, that both marketplace catalogs advertise only
``codemap-py``, and that an install-shaped plugin cache holding BOTH the legacy and renamed identities resolves to
``codemap-py`` alone (and the dual state is detectable), and that the legacy ``.cache/codemap/`` index layout is
retained (the resolver is unchanged, so existing indexes keep resolving after the rename).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest import mock

import pytest
from _scan_helpers import IGNORE_RUNNER_THREAD_FORK_WARNING

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _PLUGIN_ROOT.parents[1]
_BIN = _PLUGIN_ROOT / "bin"
_SRC = _PLUGIN_ROOT / "src"
for _p in (_BIN, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _index_identity  # noqa: E402  (needs the bin path insert above)
from codemap_py import graph, index_paths  # noqa: E402  (needs the src path insert above)


def _seed_plugin_cache(home: Path, entries: dict[str, str]) -> Path:
    """Create install-shaped ``borda-ai-rig/<plugin>/<version>`` cache dirs; return the cache base."""
    cache = home / ".claude" / "plugins" / "cache"
    for plugin, version in entries.items():
        (cache / "borda-ai-rig" / plugin / version).mkdir(parents=True)
    return cache


def _plugin_names(marketplace: Path) -> set[str]:
    """Return the set of plugin ``name`` entries in a marketplace catalog."""
    catalog = json.loads(marketplace.read_text(encoding="utf-8"))
    return {entry["name"] for entry in catalog.get("plugins", [])}


def _parsed_names(parse_mock: mock.MagicMock) -> list[str]:
    """Return the file path of every call recorded on the patched ``_parse_file``."""
    return [str(call.args[0]) for call in parse_mock.call_args_list]


class TestDualIdentityBan:
    """The repository and the plugin cache never carry the legacy and renamed identities together."""

    @pytest.mark.packaging
    def test_repo_tracks_exactly_one_plugin_identity(self) -> None:
        """Only ``plugins/codemap-py`` exists on disk; the legacy ``plugins/codemap`` directory is gone.

        A leftover legacy directory would let two plugins register the same skills and hooks after the rename.
        """
        assert (_REPO_ROOT / "plugins" / "codemap-py").is_dir()
        assert not (_REPO_ROOT / "plugins" / "codemap").exists()

    @pytest.mark.parametrize("marketplace_rel", [".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"])
    @pytest.mark.packaging
    def test_marketplace_advertises_codemap_py_only(self, marketplace_rel: str) -> None:
        """Each marketplace catalog lists ``codemap-py`` and never the legacy ``codemap`` name.

        Both the Claude and the agents catalog are checked, since a stale entry in either would offer users the retired
        plugin name.
        """
        names = _plugin_names(_REPO_ROOT / marketplace_rel)

        assert "codemap-py" in names
        assert "codemap" not in names

    @pytest.mark.packaging
    def test_resolver_selects_codemap_py_when_both_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With BOTH legacy and renamed plugin caches present, the shipped resolver picks ``codemap-py``.

        An upgraded user can have both directories under the plugin cache; resolution must still land on the renamed
        plugin's version, never on the legacy one.
        """
        home = tmp_path / "home"
        _seed_plugin_cache(home, {"codemap": "1.0.0", "codemap-py": "0.25.0"})
        monkeypatch.setenv("HOME", str(home))
        # Path.home() reads USERPROFILE first on Windows (then HOMEDRIVE/HOMEPATH), so HOME alone
        # leaves the resolver pointed at the real CI home and the seeded cache is never found.
        monkeypatch.setenv("USERPROFILE", str(home))

        resolved = index_paths.resolve_plugin_root(None)

        assert resolved is not None
        assert (resolved.parent.name, resolved.name) == ("codemap-py", "0.25.0")

    @pytest.mark.parametrize(
        ("entries", "expected"),
        [
            pytest.param({"codemap": "1.0.0", "codemap-py": "0.25.0"}, "dual_plugin_identity", id="both-cached"),
            pytest.param({"codemap-py": "0.25.0"}, None, id="only-renamed-cached"),
        ],
    )
    @pytest.mark.packaging
    def test_detect_dual_identity(self, tmp_path: Path, entries: dict[str, str], expected: str | None) -> None:
        """The shipped detector names the violation when both identities coexist and is silent otherwise.

        The legacy-plus-renamed cache is the broken state to flag; a cache holding only ``codemap-py`` is the healthy
        post-migration state and must report nothing.
        """
        cache_base = _seed_plugin_cache(tmp_path / "home", entries)

        assert index_paths.detect_dual_identity(cache_base) == expected


class TestLegacyIndexLayout:
    """The index keeps living under ``.cache/codemap`` so indexes built before the rename still resolve."""

    @pytest.mark.packaging
    def test_index_subdir_is_legacy_cache_codemap(self) -> None:
        """The resolver's index subdir constant is still ``.cache/codemap``.

        Renaming the plugin must not rename the on-disk cache directory, or every existing index would be orphaned.
        """
        assert _index_identity.INDEX_SUBDIR == Path(".cache", "codemap")

    @pytest.mark.packaging
    def test_resolver_places_index_under_legacy_cache(self, tmp_path: Path) -> None:
        """A default resolution nests the index file ``<root name>.json`` under ``.cache/codemap``.

        Complements the constant check by proving the resolver actually applies it to a real root.
        """
        identity = _index_identity.resolve_index(root=tmp_path, index_dir_override=None)

        assert identity.index_dir.parts[-2:] == (".cache", "codemap")
        assert identity.index_path.name == f"{tmp_path.name}.json"


# --- .pyi migration: exactly one rebuild via file_shas drift, then stable reuse ----


def _canonical(index: dict) -> str:
    """Serialise an index ignoring volatile fields, for byte-stability comparison."""
    stable = {k: v for k, v in index.items() if k not in ("scanned_at", "git_sha")}
    return json.dumps(stable, sort_keys=True, ensure_ascii=False)


def _to_legacy_shape(index: dict) -> dict:
    """Reshape a fresh index into a plausible pre-.pyi ``codemap`` index.

    Strips every ``.pyi`` from ``file_shas`` and drops the stub-derived state so the
    result matches what the pre-migration scanner produced: stubs never discovered,
    no ``stub_only`` modules, no ``has_stub``/``shadowed_stubs`` fields. The migration's
    one-time rebuild is then driven purely by the reappearance of ``.pyi`` in ``file_shas``.
    """
    legacy = dict(index)
    legacy["file_shas"] = {k: v for k, v in index["file_shas"].items() if not k.endswith(".pyi")}
    legacy["modules"] = [
        {k: v for k, v in m.items() if k != "has_stub"} for m in index["modules"] if not m.get("stub_only")
    ]
    legacy.pop("shadowed_stubs", None)
    legacy.pop("casefold_collisions", None)
    return legacy


@pytest.fixture(name="legacy_pyi_project")
def _legacy_pyi_project(tmp_path: Path, corpus_pyi_dir: Path) -> tuple[Path, dict]:
    """Copy the .pyi corpus and reshape a fresh scan into a pre-migration legacy index."""
    root = tmp_path / "proj"
    shutil.copytree(corpus_pyi_dir, root)
    legacy = _to_legacy_shape(graph.scan(root))
    return root, legacy


@pytest.fixture(name="migrated_index")
def _migrated_index(legacy_pyi_project: tuple[Path, dict]) -> tuple[Path, dict]:
    """Run the one-time .pyi migration scan and return the project root plus its rebuilt index."""
    root, legacy = legacy_pyi_project
    rebuilt = graph.incremental_scan(root, legacy)
    return root, rebuilt


class TestPyiMigration:
    """Adding ``.pyi`` discovery rebuilds a legacy index once, then reuses it byte-stable."""

    @pytest.mark.packaging
    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_legacy_shape_excludes_pyi_and_stub_fields(self, legacy_pyi_project: tuple[Path, dict]) -> None:
        """The simulated pre-migration index carries no ``.pyi`` or stub-derived fields.

        Confirms the reshaping in ``_to_legacy_shape`` reflects a real pre-migration scanner run, where stubs were never
        discovered, before the migration scan asserted below is exercised.
        """
        _root, legacy = legacy_pyi_project

        assert not any(k.endswith(".pyi") for k in legacy["file_shas"])
        assert not any(m.get("stub_only") for m in legacy["modules"])

    @mock.patch.object(graph, "_parse_file", autospec=True, side_effect=graph._parse_file)
    @pytest.mark.packaging
    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_migration_scan_parses_pyi_exactly_once(
        self, parse_mock: mock.MagicMock, legacy_pyi_project: tuple[Path, dict]
    ) -> None:
        """The first scan after the .pyi migration rebuilds by parsing the new stub set exactly once.

        Executable oracle: a parse-invocation counter proves only the newly-discovered ``.pyi``
        files are parsed, each at most once, and the rebuilt index now carries stub fields.
        """
        root, legacy = legacy_pyi_project

        rebuilt = graph.incremental_scan(root, legacy)

        names = _parsed_names(parse_mock)
        assert names, "the migration scan must rebuild (parse the new .pyi set)"
        assert all(n.endswith(".pyi") for n in names), names
        assert len(names) == len(set(names)), "each stub parsed at most once"
        assert any(m.get("stub_only") for m in rebuilt["modules"]), "stubs now indexed"
        assert any(k.endswith(".pyi") for k in rebuilt["file_shas"]), "stubs now in the hash set"

    @mock.patch.object(graph, "_parse_file", autospec=True, side_effect=graph._parse_file)
    @pytest.mark.packaging
    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_stable_reuse_reparses_nothing(self, parse_mock: mock.MagicMock, migrated_index: tuple[Path, dict]) -> None:
        """A scan following the migration reuses the rebuilt index byte-stable with zero reparsing.

        Starting from the already-migrated index with no further drift, the next incremental scan must not touch the
        parser and must reproduce identical canonical bytes.
        """
        root, rebuilt = migrated_index
        parse_mock.reset_mock()

        stable = graph.incremental_scan(root, rebuilt)

        assert _parsed_names(parse_mock) == [], "stable reuse must not re-parse any file"
        assert _canonical(stable) == _canonical(rebuilt), "index bytes stable across reuse"
