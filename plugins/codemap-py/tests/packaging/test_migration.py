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

import pytest


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


# --- dual-identity ban -----------------------------------------------------


@pytest.mark.packaging
def test_repo_tracks_exactly_one_plugin_identity() -> None:
    """Exactly one of plugins/codemap or plugins/codemap-py exists on disk."""
    legacy = _REPO_ROOT / "plugins" / "codemap"
    renamed = _REPO_ROOT / "plugins" / "codemap-py"
    assert renamed.is_dir()
    assert not legacy.exists()


@pytest.mark.parametrize("marketplace_rel", [".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"])
@pytest.mark.packaging
def test_marketplace_advertises_codemap_py_only(marketplace_rel: str) -> None:
    """Marketplace catalogs list codemap-py and never the legacy codemap name."""
    names = _plugin_names(_REPO_ROOT / marketplace_rel)
    assert "codemap-py" in names
    assert "codemap" not in names


# --- legacy cache path retained --------------------------------------------


@pytest.mark.packaging
def test_index_subdir_is_legacy_cache_codemap() -> None:
    """The resolver's index subdir constant is still ``.cache/codemap``."""
    assert _index_identity.INDEX_SUBDIR == Path(".cache", "codemap")


@pytest.mark.packaging
def test_resolver_places_index_under_legacy_cache(tmp_path: Path) -> None:
    """A default resolution nests the index under ``.cache/codemap`` for a root."""
    identity = _index_identity.resolve_index(root=tmp_path, index_dir_override=None)
    assert identity.index_dir.parts[-2:] == (".cache", "codemap")
    assert identity.index_path.name == f"{tmp_path.name}.json"


# --- install-shaped dual-identity ban --------------------------------------


@pytest.mark.packaging
def test_resolver_selects_codemap_py_when_both_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With BOTH legacy and renamed plugin caches present, the shipped resolver picks codemap-py."""
    home = tmp_path / "home"
    _seed_plugin_cache(home, {"codemap": "1.0.0", "codemap-py": "0.25.0"})
    monkeypatch.setenv("HOME", str(home))
    # Path.home() reads USERPROFILE first on Windows (then HOMEDRIVE/HOMEPATH), so HOME alone
    # leaves the resolver pointed at the real CI home and the seeded cache is never found.
    monkeypatch.setenv("USERPROFILE", str(home))
    resolved = index_paths.resolve_plugin_root(None)
    assert resolved is not None
    assert resolved.parent.name == "codemap-py"
    assert resolved.name == "0.25.0"


@pytest.mark.packaging
def test_shipped_detect_dual_identity_fires_when_both_cached(tmp_path: Path) -> None:
    """The shipped migration utility names the violation when both identities coexist."""
    cache_base = _seed_plugin_cache(tmp_path / "home", {"codemap": "1.0.0", "codemap-py": "0.25.0"})
    assert index_paths.detect_dual_identity(cache_base) == "dual_plugin_identity"


@pytest.mark.packaging
def test_shipped_detect_dual_identity_clean_when_only_renamed_cached(tmp_path: Path) -> None:
    """The shipped detector is silent when only codemap-py is cached."""
    cache_base = _seed_plugin_cache(tmp_path / "home", {"codemap-py": "0.25.0"})
    assert index_paths.detect_dual_identity(cache_base) is None


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


class _CountingParse:
    """Wrap ``graph._parse_file`` to record which files each scan actually re-parses."""

    def __init__(self) -> None:
        """Capture the real parser so calls can be counted without changing behavior."""
        self._real = graph._parse_file
        self.names: list[str] = []

    def __call__(self, filepath: Path, root: Path, src_root: Path) -> dict:
        """Record a parsed path, then delegate to the real parser."""
        self.names.append(str(filepath))
        return self._real(filepath, root, src_root)


@pytest.mark.packaging
def test_pyi_rebuild_single_pass_then_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus_pyi_dir: Path
) -> None:
    """First scan after the .pyi migration rebuilds once; the next scan reuses it byte-stable.

    Executable oracle: a parse-invocation counter proves the ``.pyi`` set
    is parsed exactly once on the migration scan and not at all on the following scan, and a canonical-bytes comparison
    proves the index is stable.
    """
    root = tmp_path / "proj"
    shutil.copytree(corpus_pyi_dir, root)

    # A pre-migration index: no .pyi discovered, no stub fields.
    legacy = _to_legacy_shape(graph.scan(root))
    assert not any(k.endswith(".pyi") for k in legacy["file_shas"])
    assert not any(m.get("stub_only") for m in legacy["modules"])

    counter = _CountingParse()
    monkeypatch.setattr(graph, "_parse_file", counter)

    # Migration scan: the newly-discovered .pyi files drift file_shas and are parsed once.
    rebuilt = graph.incremental_scan(root, legacy)
    assert counter.names, "the migration scan must rebuild (parse the new .pyi set)"
    assert all(n.endswith(".pyi") for n in counter.names), counter.names
    assert len(counter.names) == len(set(counter.names)), "each stub parsed at most once"
    assert any(m.get("stub_only") for m in rebuilt["modules"]), "stubs now indexed"
    assert any(k.endswith(".pyi") for k in rebuilt["file_shas"]), "stubs now in the hash set"

    # Stable reuse: no drift -> zero parses, identical bytes.
    counter.names.clear()
    stable = graph.incremental_scan(root, rebuilt)
    assert counter.names == [], "stable reuse must not re-parse any file"
    assert _canonical(stable) == _canonical(rebuilt), "index bytes stable across reuse"
