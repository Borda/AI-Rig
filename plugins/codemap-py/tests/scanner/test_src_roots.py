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
from _scan_helpers import write_tree

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

_MONOREPO_FILES = {
    "pyproject.toml": '[tool.codemap]\nsrc_roots = ["libs/core/src", "services/api/src"]\n',
    "libs/core/src/pkg_a/__init__.py": "",
    "libs/core/src/pkg_a/mod_a.py": "def a():\n    return 1\n",
    "services/api/src/pkg_b/__init__.py": "",
    "services/api/src/pkg_b/mod_b.py": "def b():\n    return 2\n",
}
_MONOREPO_ROOTS = ("libs/core/src", "services/api/src")


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


@pytest.fixture(name="monorepo_index", scope="module")
def _monorepo_index(tmp_path_factory: pytest.TempPathFactory, scan_index: Path) -> dict:
    """Index of the two-source-root monorepo, scanned once; tests only read it."""
    root = tmp_path_factory.mktemp("monorepo")
    write_tree(root, _MONOREPO_FILES)
    return _scan_and_load(scan_index, root)


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
        """The ordered ``src_roots`` array of the ``[tool.codemap]`` table is extracted, nothing from other tables.

        Declaration order is the collision priority, so it must survive parsing; a same-named key in another tool's
        table must not be mistaken for codemap configuration.
        """
        assert _parse_codemap_src_roots_toml(text) == expected


class TestLoadSrcRoots:
    """load_src_roots resolves existing directories in priority order."""

    def test_returns_existing_dirs_in_declaration_order(self, tmp_path: Path):
        """Only directories that exist are returned, ordered as declared.

        Both roots of the monorepo exist on disk; they come back as absolute paths in the order of the config.
        """
        write_tree(tmp_path, _MONOREPO_FILES)

        roots = load_src_roots(tmp_path)

        assert [p.relative_to(tmp_path).as_posix() for p in roots] == list(_MONOREPO_ROOTS)

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
        """Only declared roots that exist and stay inside the project root survive.

        A stale entry must not break the scan, and a ``..`` entry must not let the config point the scanner at files
        outside the project.
        """
        write_tree(tmp_path, {"pyproject.toml": f"[tool.codemap]\nsrc_roots = {declared}\n"})
        (tmp_path / existing_dir).mkdir(parents=True)

        roots = load_src_roots(tmp_path)

        assert [p.relative_to(tmp_path).as_posix() for p in roots] == [existing_dir]

    def test_no_pyproject_returns_empty(self, tmp_path: Path):
        """A project without pyproject.toml has no configured roots.

        The single-root detection then applies, exactly as before ``src_roots`` existed.
        """
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
        """A file under a configured root is named relative to that root, by priority.

        Module names are derived from the root a file lives under, so the first matching root in config order must win.
        """
        roots = (tmp_path / "libs" / "core" / "src", tmp_path / "services" / "api" / "src")

        assert _effective_src_root(tmp_path / relative_file, roots, tmp_path) == roots[root_index]

    @pytest.mark.parametrize(
        ("configured", "relative_file"),
        [
            # A file under no configured root falls back to the default root.
            pytest.param(("libs/core/src",), "other/stray.py", id="no-root-matches"),
            # With no configured roots every file uses the default (single-root) path.
            pytest.param((), "pkg/mod.py", id="no-configured-roots"),
        ],
    )
    def test_falls_back_to_default_root(self, tmp_path: Path, configured: tuple[str, ...], relative_file: str):
        """A file under no configured root (or with none configured) uses the default root.

        Stray files and projects without ``src_roots`` keep the pre-monorepo single-root naming.
        """
        roots = tuple(tmp_path / rel for rel in configured)

        assert _effective_src_root(tmp_path / relative_file, roots, tmp_path) == tmp_path


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
        """A single string or a tuple becomes a tuple of non-empty entries in priority order.

        Callers still pass the legacy single-string form; both shapes must feed the same ranking code.
        """
        assert _src_root_rels(value) == expected


class TestUnderRootRank:
    """_under_root_rank ranks a path by the first source root it lies under."""

    @pytest.mark.parametrize(
        ("path", "roots", "expected_rank"),
        [
            # A path under the highest-priority root ranks 0.
            pytest.param("libs/core/src/pkg/m.py", _MONOREPO_ROOTS, 0, id="first-root-ranks-zero"),
            # A path under the second root ranks 1 (after the first).
            pytest.param("services/api/src/pkg/m.py", _MONOREPO_ROOTS, 1, id="second-root-ranks-one"),
            # A path under no configured root ranks after every configured root.
            pytest.param("stray/m.py", _MONOREPO_ROOTS, 2, id="no-root-ranks-after-all"),
            # One root reproduces the original under(0)/outside(1) ranking.
            pytest.param("src/m.py", ("src",), 0, id="single-root-under-ranks-zero"),
            pytest.param("copy/m.py", ("src",), 1, id="single-root-outside-ranks-one"),
        ],
    )
    def test_rank_is_the_index_of_the_first_root_the_path_lies_under(self, path, roots, expected_rank):
        """A path ranks by the first source root it lies under, or after all roots when under none.

        The rank is the primary key of the collision winner choice, so each position, the no-root position and the
        single-root special case must be pinned.
        """
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
                _MONOREPO_ROOTS,
                id="earlier-root-beats-later-root",
            ),
            # A path under any configured root beats a stray copy under none.
            pytest.param("services/api/src/pkg/m.py", "vendor/pkg/m.py", _MONOREPO_ROOTS, id="any-root-beats-non-root"),
            # The pre-existing single-rel-string call form keeps working.
            pytest.param("src/m.py", "copy/m.py", "src", id="legacy-single-string-still-supported"),
        ],
    )
    def test_winner_sorts_before_loser(self, winner: str, loser: str, roots):
        """A path under a higher-priority configured root sorts before a lower-priority or stray one.

        Config order, not path length, decides between roots; the legacy single-string argument must behave the same.
        """
        assert _dedup_key(winner, roots) < _dedup_key(loser, roots)


class TestDedupModulesRootAware:
    """_dedup_modules resolves collisions using multi-root priority."""

    @pytest.mark.parametrize(
        ("name", "paths", "kept", "dropped"),
        [
            # A configured-root path beats a stray copy wherever the stray appears in the input.
            pytest.param(
                "pkg_b.mod_b",
                ["vendor/pkg_b/mod_b.py", "services/api/src/pkg_b/mod_b.py"],
                "services/api/src/pkg_b/mod_b.py",
                ["vendor/pkg_b/mod_b.py"],
                id="stray-copy-loses",
            ),
            pytest.param(
                "pkg_b.mod_b",
                ["services/api/src/pkg_b/mod_b.py", "vendor/pkg_b/mod_b.py"],
                "services/api/src/pkg_b/mod_b.py",
                ["vendor/pkg_b/mod_b.py"],
                id="stray-copy-loses-reversed-input",
            ),
            # The same name under two configured roots goes to the first-listed root.
            pytest.param(
                "shared.mod",
                ["services/api/src/shared/mod.py", "libs/core/src/shared/mod.py"],
                "libs/core/src/shared/mod.py",
                ["services/api/src/shared/mod.py"],
                id="earlier-root-wins-over-later-root",
            ),
        ],
    )
    def test_root_priority_decides_winner(self, name: str, paths: list[str], kept: str, dropped: list[str]):
        """The path under the highest-priority configured root wins and the loser is recorded as dropped.

        Input order mimics filesystem walk order, which is arbitrary; the survivor must follow root priority only.
        """
        entries = [{"name": name, "path": path} for path in paths]

        kept_entries, collisions = _dedup_modules(entries, _MONOREPO_ROOTS)

        assert [m["path"] for m in kept_entries] == [kept]
        assert collisions == [{"name": name, "kept": kept, "dropped": dropped}]


# ── end-to-end scan behaviour ─────────────────────────────────────────────────────


class TestMonorepoScan:
    """A real scan of a two-root monorepo names both packages and records the roots."""

    def test_dotted_names_derive_from_each_root(self, monorepo_index: dict):
        """Modules under each configured root get names relative to that root.

        ``pkg_a`` lives under ``libs/core/src`` and ``pkg_b`` under ``services/api/src``; neither name may carry the
        directory prefix of its root.
        """
        names = {m["name"] for m in monorepo_index["modules"]}

        assert {"pkg_a", "pkg_a.mod_a", "pkg_b", "pkg_b.mod_b"}.issubset(names)

    def test_meta_records_effective_roots(self, monorepo_index: dict):
        """The index meta lists both configured source roots and flags a src layout.

        Downstream queries read ``src_roots`` to reproduce the naming without rescanning.
        """
        assert monorepo_index["src_roots"] == list(_MONOREPO_ROOTS)
        assert monorepo_index["src_layout"] is True

    def test_root_path_wins_collision_over_stray_copy(self, tmp_path: Path, scan_index):
        """A stray copy of a package colliding with a root path loses deterministically.

        The stray sits directly at the project root, so single-root fallback names it ``pkg_b.mod_b`` — colliding with
        the real package under ``services/api/src``. The configured-root path must win, since it ranks under a source
        root and the stray is under none.
        """
        write_tree(
            tmp_path, {**_MONOREPO_FILES, "pkg_b/__init__.py": "", "pkg_b/mod_b.py": "def b():\n    return 99\n"}
        )

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
        """Without ``src_roots`` the scan names packages by the detected default root and records its layout.

        A conventional ``src/`` layout and a flat repository are scanned with no codemap configuration; naming and the
        recorded layout must match what single-root detection always produced.
        """
        write_tree(
            tmp_path, {f"{package_dir}/__init__.py": "", f"{package_dir}/{module_file}": "def f():\n    return 0\n"}
        )

        index = _scan_and_load(scan_index, tmp_path)

        assert expected_names.issubset({m["name"] for m in index["modules"]})
        assert index["src_roots"] == expected_roots
        assert index["src_layout"] is expected_layout

    def test_resolve_src_roots_collapses_to_single_root(self, tmp_path: Path):
        """Without config, the resolved context uses only the detected default root.

        No configured roots means no multi-root dedup ranking: ``dedup_rels`` must be empty so collisions use the
        legacy ordering.
        """
        write_tree(tmp_path, {"pkg/__init__.py": ""})

        ctx = _resolve_src_roots(tmp_path)

        assert ctx.configured == ()
        assert ctx.dedup_rels(tmp_path) == ()


class TestPackageIdentityOutsideDetectedRoot:
    """Package descendants outside a preferred root retain importable names."""

    @pytest.mark.parametrize(
        ("files", "worker", "expected"),
        [
            pytest.param(
                {"src/production/__init__.py": "", "tests/tests_fabric/__init__.py": ""},
                "tests/tests_fabric/worker.py",
                "tests_fabric.worker",
                id="tests-prefix",
            ),
            pytest.param(
                {"src/production/__init__.py": "", "tests/tests_fabric/__init__.pyi": ""},
                "tests/tests_fabric/worker.py",
                "tests_fabric.worker",
                id="stub-package-init",
            ),
            pytest.param(
                {"src/production/__init__.py": "", "namespace/tests_fabric/__init__.py": ""},
                "namespace/tests_fabric/worker.py",
                "tests_fabric.worker",
                id="namespace-prefix",
            ),
            pytest.param(
                {"src/production/__init__.py": "", "tests/__init__.py": "", "tests/tests_fabric/__init__.py": ""},
                "tests/tests_fabric/worker.py",
                "tests.tests_fabric.worker",
                id="real-tests-package",
            ),
            pytest.param(
                {
                    "pyproject.toml": '[tool.codemap]\nsrc_roots = ["configured/src"]\n',
                    "configured/src/production/__init__.py": "",
                    "ignored/tests_fabric/__init__.py": "",
                },
                "ignored/tests_fabric/worker.py",
                "tests_fabric.worker",
                id="configured-root",
            ),
        ],
    )
    def test_nested_package_drops_only_non_package_prefix(
        self, tmp_path: Path, files: dict[str, str], worker: str, expected: str
    ) -> None:
        """A source-root mismatch uses the outermost real package as the import root.

        ``tests_fabric`` sits outside the preferred source root under a plain directory (or a real package); only the
        non-package prefix may be dropped from its importable name, with ``.py`` and ``.pyi`` package inits alike.
        """
        write_tree(tmp_path, {**files, worker: "def run():\n    return 1\n"})

        context = _resolve_src_roots(tmp_path)
        entry = _parse_file(tmp_path / worker, tmp_path, context.name_root_for(tmp_path / worker))

        assert entry["name"] == expected


class TestMonorepoIncrementalScan:
    """Incremental re-scan preserves multi-root naming for changed files."""

    def test_incremental_names_new_file_under_its_root(self, tmp_path: Path, scan_index):
        """A file added under the second root after the initial scan is named from that root.

        A full scan indexes the monorepo, ``extra.py`` is added under ``services/api/src`` and only ``--incremental``
        runs; the new module must get its root-relative name and the recorded roots must stay intact.
        """
        write_tree(tmp_path, _MONOREPO_FILES)
        _scan_and_load(scan_index, tmp_path)
        write_tree(tmp_path, {"services/api/src/pkg_b/extra.py": "def c():\n    return 3\n"})

        index = _scan_and_load(scan_index, tmp_path, "--incremental")

        assert "pkg_b.extra" in {m["name"] for m in index["modules"]}
        assert index["src_roots"] == list(_MONOREPO_ROOTS)
