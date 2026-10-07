"""Integration + unit tests for monorepo multi-source-root awareness in scan-index.

Covers ``[tool.codemap] src_roots``: module naming derives from the matching source
root, collision resolution prefers a path under any configured root (with declaration
order as priority), and the effective roots are recorded in the index meta. A no-config
project must behave identically to the single-root detection it always used.

Monorepo fixture layout::

    libs/core/src/pkg_a/__init__.py   (pkg_a)          — first-priority root
    libs/core/src/pkg_a/mod_a.py      (pkg_a.mod_a)
    services/api/src/pkg_b/__init__.py (pkg_b)          — second-priority root
    services/api/src/pkg_b/mod_b.py   (pkg_b.mod_b)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# The scan-index implementation now lives in the package:
# discovery/parsing (incl. former bin/_exclusions.py content) -> codemap_py.scanner,
# graph/dedup/index-write -> codemap_py.graph. bin/scan-index is now a thin launcher,
# so unit-level access imports the package modules directly.
_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import codemap_py.graph as _graph_mod  # noqa: E402  (needs the sys.path insert above)
import codemap_py.scanner as _scanner_mod  # noqa: E402

_effective_src_root = _scanner_mod._effective_src_root
load_src_roots = _scanner_mod.load_src_roots
_parse_codemap_src_roots_toml = _scanner_mod._parse_codemap_src_roots_toml
_src_root_rels = _graph_mod._src_root_rels
_under_root_rank = _graph_mod._under_root_rank
_dedup_key = _graph_mod._dedup_key
_dedup_modules = _graph_mod._dedup_modules
_resolve_src_roots = _graph_mod._resolve_src_roots
_parse_file = _scanner_mod._parse_file


_PYPROJECT_TWO_ROOTS = '[tool.codemap]\nsrc_roots = ["libs/core/src", "services/api/src"]\n'


def _materialize_monorepo(root: Path) -> None:
    """Write a two-source-root monorepo tree with a pyproject declaring both roots."""
    (root / "pyproject.toml").write_text(_PYPROJECT_TWO_ROOTS)
    pkg_a = root / "libs" / "core" / "src" / "pkg_a"
    pkg_a.mkdir(parents=True)
    (pkg_a / "__init__.py").write_text("")
    (pkg_a / "mod_a.py").write_text("def a():\n    return 1\n")
    pkg_b = root / "services" / "api" / "src" / "pkg_b"
    pkg_b.mkdir(parents=True)
    (pkg_b / "__init__.py").write_text("")
    (pkg_b / "mod_b.py").write_text("def b():\n    return 2\n")


def _scan_and_load(scan_index: Path, root: Path, *extra: str) -> dict:
    """Run scan-index over *root* and return the parsed index dict."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr
    index_path = root / ".cache" / "codemap" / f"{root.name}.json"
    assert index_path.exists(), "scan-index did not produce an index file"
    return json.loads(index_path.read_text())


# ── config parsing / loading ─────────────────────────────────────────────────────


class TestParseSrcRootsToml:
    """_parse_codemap_src_roots_toml extracts the ordered src_roots array."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # A multi-line array yields entries in declaration order.
            pytest.param(
                '[tool.codemap]\nsrc_roots = [\n  "a/src",\n  "b/src",\n]\n',
                ["a/src", "b/src"],
                id="multi-line-array-preserves-order",
            ),
            # A [tool.codemap] section without src_roots yields no entries.
            pytest.param('[tool.codemap]\nexclude = ["x"]\n', [], id="absent-key-returns-empty"),
            # src_roots under a different table is not picked up.
            pytest.param('[tool.other]\nsrc_roots = ["x"]\n', [], id="wrong-section-ignored"),
        ],
    )
    def test_src_roots_array_is_extracted(self, text: str, expected: list[str]):
        """The ordered ``src_roots`` array of the ``[tool.codemap]`` table is extracted, nothing from other tables."""
        assert _parse_codemap_src_roots_toml(text) == expected


class TestLoadSrcRoots:
    """load_src_roots resolves existing directories in priority order."""

    def test_returns_existing_dirs_in_declaration_order(self, tmp_path: Path):
        """Only directories that exist are returned, ordered as declared."""
        _materialize_monorepo(tmp_path)
        roots = load_src_roots(tmp_path)
        rels = [p.relative_to(tmp_path).as_posix() for p in roots]
        assert rels == ["libs/core/src", "services/api/src"]

    @pytest.mark.parametrize(
        ("declared", "existing_dir"),
        [
            # A declared root that does not exist on disk is skipped.
            pytest.param('["exists/src", "ghost/src"]', "exists/src", id="missing-dirs-dropped"),
            # A src_root entry that escapes the project root is dropped.
            pytest.param('["../outside", "in/src"]', "in/src", id="escaping-entry-ignored"),
        ],
    )
    def test_unusable_entries_dropped(self, tmp_path: Path, declared: str, existing_dir: str):
        """Only declared roots that exist and stay inside the project root survive."""
        (tmp_path / "pyproject.toml").write_text(f"[tool.codemap]\nsrc_roots = {declared}\n")
        (tmp_path / existing_dir).mkdir(parents=True)
        roots = load_src_roots(tmp_path)
        assert [p.relative_to(tmp_path).as_posix() for p in roots] == [existing_dir]

    def test_no_pyproject_returns_empty(self, tmp_path: Path):
        """A project without pyproject.toml has no configured roots."""
        assert load_src_roots(tmp_path) == []


# ── pure naming / ranking helpers ─────────────────────────────────────────────────


class TestEffectiveSrcRoot:
    """_effective_src_root maps a file to its first-matching configured root."""

    @pytest.mark.parametrize(
        ("relative_file", "root_index"),
        [
            # A file under the first root is named relative to that root.
            pytest.param("libs/core/src/pkg_a/mod_a.py", 0, id="first-matching-root-by-priority"),
            # A file only under the second root uses the second root.
            pytest.param("services/api/src/pkg_b/mod_b.py", 1, id="second-root-when-first-does-not-match"),
        ],
    )
    def test_file_maps_to_its_matching_root(self, tmp_path: Path, relative_file: str, root_index: int):
        """A file under a configured root is named relative to that root, by priority."""
        roots = (tmp_path / "libs" / "core" / "src", tmp_path / "services" / "api" / "src")
        assert _effective_src_root(tmp_path / relative_file, roots, tmp_path) == roots[root_index]

    def test_falls_back_to_default_when_no_root_matches(self, tmp_path: Path):
        """A file under no configured root falls back to the default root."""
        r0 = tmp_path / "libs" / "core" / "src"
        fp = tmp_path / "other" / "stray.py"
        assert _effective_src_root(fp, (r0,), tmp_path) == tmp_path

    def test_no_configured_roots_uses_default(self, tmp_path: Path):
        """With no configured roots every file uses the default (single-root) path."""
        fp = tmp_path / "pkg" / "mod.py"
        assert _effective_src_root(fp, (), tmp_path) == tmp_path


class TestSrcRootRels:
    """_src_root_rels normalises single-string and tuple forms, dropping empties."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # A lone rel string becomes a one-element tuple.
            pytest.param("src", ("src",), id="single-string-wrapped"),
            # The project-root sentinel carries no ranking signal and is dropped.
            pytest.param("", (), id="empty-string-dropped"),
            # Empty entries inside a priority tuple are removed, order preserved.
            pytest.param(
                ("libs/core/src", "", "services/api/src"),
                ("libs/core/src", "services/api/src"),
                id="tuple-filters-empty-entries",
            ),
        ],
    )
    def test_value_is_normalised_to_a_tuple(self, value, expected):
        """A single string or a tuple becomes a tuple of non-empty entries in priority order."""
        assert _src_root_rels(value) == expected


class TestUnderRootRank:
    """_under_root_rank ranks a path by the first source root it lies under."""

    @pytest.mark.parametrize(
        ("path", "roots", "expected_rank"),
        [
            # A path under the highest-priority root ranks 0.
            pytest.param(
                "libs/core/src/pkg/m.py", ("libs/core/src", "services/api/src"), 0, id="first-root-ranks-zero"
            ),
            # A path under the second root ranks 1 (after the first).
            pytest.param(
                "services/api/src/pkg/m.py", ("libs/core/src", "services/api/src"), 1, id="second-root-ranks-one"
            ),
            # A path under no configured root ranks after every configured root.
            pytest.param("stray/m.py", ("libs/core/src", "services/api/src"), 2, id="no-root-ranks-after-all"),
            # One root reproduces the original under(0)/outside(1) ranking.
            pytest.param("src/m.py", ("src",), 0, id="single-root-under-ranks-zero"),
            pytest.param("copy/m.py", ("src",), 1, id="single-root-outside-ranks-one"),
        ],
    )
    def test_rank_is_the_index_of_the_first_root_the_path_lies_under(self, path, roots, expected_rank):
        """A path ranks by the first source root it lies under, or after all roots when under none."""
        assert _under_root_rank(path, roots) == expected_rank


class TestDedupKeyMultiRoot:
    """_dedup_key ranks candidate paths across multiple priority-ordered roots."""

    @pytest.mark.parametrize(
        ("winner", "loser", "roots"),
        [
            # A path under the first root outranks one under the second, regardless of length.
            pytest.param(
                "libs/core/src/deep/nested/m.py",
                "services/api/src/m.py",
                ("libs/core/src", "services/api/src"),
                id="earlier-root-beats-later-root",
            ),
            # A path under any configured root beats a stray copy under none.
            pytest.param(
                "services/api/src/pkg/m.py",
                "vendor/pkg/m.py",
                ("libs/core/src", "services/api/src"),
                id="any-root-beats-non-root",
            ),
            # The pre-existing single-rel-string call form keeps working.
            pytest.param("src/m.py", "copy/m.py", "src", id="legacy-single-string-still-supported"),
        ],
    )
    def test_winner_sorts_before_loser(self, winner: str, loser: str, roots):
        """A path under a higher-priority configured root sorts before a lower-priority or stray one."""
        assert _dedup_key(winner, roots) < _dedup_key(loser, roots)


class TestDedupModulesRootAware:
    """_dedup_modules resolves collisions using multi-root priority."""

    @pytest.mark.parametrize("reverse", [False, True])
    def test_root_path_beats_stray_copy_deterministically(self, reverse: bool):
        """A configured-root path always wins over a stray copy across input orders."""
        entries = [
            {"name": "pkg_b.mod_b", "path": "vendor/pkg_b/mod_b.py"},
            {"name": "pkg_b.mod_b", "path": "services/api/src/pkg_b/mod_b.py"},
        ]
        roots = ("libs/core/src", "services/api/src")
        order = list(reversed(entries)) if reverse else entries
        kept, collisions = _dedup_modules(list(order), roots)
        assert len(kept) == 1
        assert kept[0]["path"] == "services/api/src/pkg_b/mod_b.py"
        assert collisions == [
            {
                "name": "pkg_b.mod_b",
                "kept": "services/api/src/pkg_b/mod_b.py",
                "dropped": ["vendor/pkg_b/mod_b.py"],
            }
        ]

    def test_earlier_root_wins_over_later_root(self):
        """When the same name appears under two roots, the first-listed root wins."""
        entries = [
            {"name": "shared.mod", "path": "services/api/src/shared/mod.py"},
            {"name": "shared.mod", "path": "libs/core/src/shared/mod.py"},
        ]
        roots = ("libs/core/src", "services/api/src")
        kept, collisions = _dedup_modules(entries, roots)
        assert kept[0]["path"] == "libs/core/src/shared/mod.py"
        assert collisions[0]["kept"] == "libs/core/src/shared/mod.py"


# ── end-to-end scan behaviour ─────────────────────────────────────────────────────


class TestMonorepoScan:
    """A real scan of a two-root monorepo names both packages and records the roots."""

    def test_dotted_names_derive_from_each_root(self, tmp_path: Path, scan_index):
        """Modules under each configured root get names relative to that root."""
        _materialize_monorepo(tmp_path)
        index = _scan_and_load(scan_index, tmp_path)
        names = {m["name"] for m in index["modules"]}
        assert {"pkg_a", "pkg_a.mod_a", "pkg_b", "pkg_b.mod_b"}.issubset(names)

    def test_meta_records_effective_roots(self, tmp_path: Path, scan_index):
        """The index meta lists both configured source roots and flags a src layout."""
        _materialize_monorepo(tmp_path)
        index = _scan_and_load(scan_index, tmp_path)
        assert index["src_roots"] == ["libs/core/src", "services/api/src"]
        assert index["src_layout"] is True

    def test_root_path_wins_collision_over_stray_copy(self, tmp_path: Path, scan_index):
        """A stray copy of a package colliding with a root path loses deterministically.

        The stray sits directly at the project root, so single-root fallback names it ``pkg_b.mod_b`` — colliding with
        the real package under ``services/api/src``. The configured-root path must win, since it ranks under a source
        root and the stray is under none.
        """
        _materialize_monorepo(tmp_path)
        stray = tmp_path / "pkg_b"
        stray.mkdir()
        (stray / "__init__.py").write_text("")
        (stray / "mod_b.py").write_text("def b():\n    return 99\n")

        index = _scan_and_load(scan_index, tmp_path)
        collision = next((c for c in index["collisions"] if c["name"] == "pkg_b.mod_b"), None)
        assert collision is not None
        assert collision["kept"] == "services/api/src/pkg_b/mod_b.py"
        assert "pkg_b/mod_b.py" in collision["dropped"]
        kept_path = next(m["path"] for m in index["modules"] if m["name"] == "pkg_b.mod_b")
        assert kept_path == "services/api/src/pkg_b/mod_b.py"


class TestNoConfigRegression:
    """A project without src_roots behaves exactly as single-root detection always did."""

    @pytest.mark.parametrize(
        ("package_dir", "module_file", "expected_names", "expected_roots", "expected_layout"),
        [
            # A conventional src/ layout with no src_roots names the package under src/.
            pytest.param(
                "src/mypkg",
                "core.py",
                {"mypkg", "mypkg.core"},
                ["src"],
                True,
                id="src-root-layout-named-without-config",
            ),
            # A flat repo (package at root) records no source-root layout.
            pytest.param("pkg", "mod.py", {"pkg", "pkg.mod"}, [], False, id="flat-repo-has-no-src-layout"),
        ],
    )
    def test_layout_detected_without_config(
        self,
        tmp_path: Path,
        scan_index,
        package_dir: str,
        module_file: str,
        expected_names: set[str],
        expected_roots: list[str],
        expected_layout: bool,
    ):
        """Without ``src_roots`` the scan names packages by the detected default root and records its layout."""
        pkg = tmp_path / package_dir
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / module_file).write_text("def f():\n    return 0\n")

        index = _scan_and_load(scan_index, tmp_path)
        names = {m["name"] for m in index["modules"]}
        assert expected_names.issubset(names)
        assert index["src_roots"] == expected_roots
        assert index["src_layout"] is expected_layout

    def test_resolve_src_roots_collapses_to_single_root(self, tmp_path: Path):
        """Without config, the resolved context uses only the detected default root."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        ctx = _resolve_src_roots(tmp_path)
        assert ctx.configured == ()
        assert ctx.dedup_rels(tmp_path) == ()


class TestPackageIdentityOutsideDetectedRoot:
    """Package descendants outside a preferred root retain importable names."""

    @pytest.mark.parametrize(
        ("prefix", "prefix_is_package", "package_init", "configured_root", "expected"),
        [
            pytest.param("tests", False, "__init__.py", None, "tests_fabric.worker", id="tests-prefix"),
            pytest.param("tests", False, "__init__.pyi", None, "tests_fabric.worker", id="stub-package-init"),
            pytest.param("namespace", False, "__init__.py", None, "tests_fabric.worker", id="namespace-prefix"),
            pytest.param("tests", True, "__init__.py", None, "tests.tests_fabric.worker", id="real-tests-package"),
            pytest.param(
                "ignored", False, "__init__.py", "configured/src", "tests_fabric.worker", id="configured-root"
            ),
        ],
    )
    def test_nested_package_drops_only_non_package_prefix(
        self,
        tmp_path: Path,
        prefix: str,
        prefix_is_package: bool,
        package_init: str,
        configured_root: str | None,
        expected: str,
    ) -> None:
        """A source-root mismatch uses the outermost real package as the import root."""
        source_root = tmp_path / (configured_root or "src")
        production = source_root / "production"
        production.mkdir(parents=True)
        (production / "__init__.py").write_text("")
        if configured_root:
            (tmp_path / "pyproject.toml").write_text(f'[tool.codemap]\nsrc_roots = ["{configured_root}"]\n')

        package_parent = tmp_path / prefix
        package = package_parent / "tests_fabric"
        package.mkdir(parents=True)
        if prefix_is_package:
            (package_parent / "__init__.py").write_text("")
        (package / package_init).write_text("")
        worker = package / "worker.py"
        worker.write_text("def run():\n    return 1\n")

        context = _resolve_src_roots(tmp_path)
        entry = _parse_file(worker, tmp_path, context.name_root_for(worker))
        assert entry["name"] == expected


class TestMonorepoIncrementalScan:
    """Incremental re-scan preserves multi-root naming for changed files."""

    def test_incremental_names_new_file_under_its_root(self, tmp_path: Path, scan_index):
        """A file added under the second root after the initial scan is named from that root."""
        _materialize_monorepo(tmp_path)
        _scan_and_load(scan_index, tmp_path)

        new_mod = tmp_path / "services" / "api" / "src" / "pkg_b" / "extra.py"
        new_mod.write_text("def c():\n    return 3\n")
        index = _scan_and_load(scan_index, tmp_path, "--incremental")

        names = {m["name"] for m in index["modules"]}
        assert "pkg_b.extra" in names
        assert index["src_roots"] == ["libs/core/src", "services/api/src"]
