"""Tests for source-root detection pruning and determinism in ``_detect_src_root_from_init``.

The detector used to pair two unbounded ``rglob`` sweeps with a post-hoc ``SKIP_DIRS``
filter. Three defects followed, none of which any test covered:

* excluded subtrees were walked in full, and could win the election;
* ``[tool.codemap] exclude`` / ``.codemapignore`` were never consulted at all;
* the winner was read out of a ``set``, so it varied with ``PYTHONHASHSEED``.

Fixture layout (one real root, one decoy under an excluded name)::

    src/pkg/__init__.py                  — the intended source root is ``src``
    vendored/results/proj/src/pkg/__init__.py — decoy, excluded by name
    .cache/proj/src/pkg/__init__.py      — decoy, excluded as a dot-directory
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import codemap_py.scanner as _scanner_mod  # noqa: E402  (needs the sys.path insert above)

_detect_src_root_from_init = _scanner_mod._detect_src_root_from_init
detect_src_root = _scanner_mod.detect_src_root

_EXCLUDE_RESULTS = '[tool.codemap]\nexclude = ["results"]\n'
_DECOYS_EXCLUDED_BY_PYPROJECT = ["vendored/results/proj/src", ".cache/proj/src"]


def _write_pkg(base: Path) -> None:
    """Create a minimal ``pkg`` package under *base* so *base* reads as a source root."""
    pkg = base / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")


def _write_pkgs(root: Path, package_dirs: list[str]) -> None:
    """Create a ``pkg`` package under each relative directory of *package_dirs*."""
    for package_dir in package_dirs:
        _write_pkg(root / package_dir)


@pytest.fixture(name="decoy_project")
def _decoy_project(tmp_path: Path) -> Path:
    """Build a project whose only legitimate source root competes with two excluded decoys."""
    _write_pkgs(tmp_path, ["src", *_DECOYS_EXCLUDED_BY_PYPROJECT])
    (tmp_path / "pyproject.toml").write_text(_EXCLUDE_RESULTS, encoding="utf-8")
    return tmp_path


class TestDetectSrcRootFromInit:
    """``_detect_src_root_from_init`` prunes excluded trees and elects the same root on every run."""

    @pytest.mark.parametrize(
        ("config_file", "config_text", "decoys"),
        [
            # An excluded name in [tool.codemap] and a dot-directory both lose to the real root.
            pytest.param(
                "pyproject.toml", _EXCLUDE_RESULTS, _DECOYS_EXCLUDED_BY_PYPROJECT, id="pyproject-exclude-and-dot-dir"
            ),
            # .codemapignore entries exclude candidates, same as the pyproject key.
            pytest.param(".codemapignore", "snapshot\n", ["fixtures/snapshot/src"], id="codemapignore-entry"),
        ],
    )
    def test_excluded_subtree_cannot_win(self, tmp_path: Path, config_file: str, config_text: str, decoys: list[str]):
        """A subtree named in ``[tool.codemap] exclude`` or ``.codemapignore`` is not a source-root candidate.

        A decoy source root (a vendored result snapshot, a cache copy) sits deeper than the real ``src``; before
        pruning, such a decoy could win the election and rename every module in the index.
        """
        _write_pkgs(tmp_path, ["src", *decoys])
        (tmp_path / config_file).write_text(config_text, encoding="utf-8")

        assert _detect_src_root_from_init(tmp_path) == tmp_path / "src"

    @pytest.mark.parametrize("hash_seed", [0, 1, 2, 3])
    def test_detection_independent_of_hash_seed(self, decoy_project: Path, hash_seed: int):
        """The winner must not vary with ``PYTHONHASHSEED``.

        Candidates were collected into a ``set`` and read back by iteration order, so the detected root changed between
        runs of the same unchanged tree — and with it every module name derived from it. Run in a subprocess because a
        seed only takes effect at interpreter start.
        """
        program = "\n".join(
            [
                "import sys",
                f"sys.path.insert(0, {str(_SRC)!r})",
                "from pathlib import Path",
                "import codemap_py.scanner as scanner",
                f"print(scanner._detect_src_root_from_init(Path({str(decoy_project)!r})))",
            ]
        )

        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            # Inherit the environment: replacing it wholesale drops SystemRoot/COMSPEC/
            # PATHEXT/TEMP, and the win32 child then aborts before running with
            # "_Py_HashRandomization_Init: failed to get random numbers".
            env={**os.environ, "PYTHONHASHSEED": str(hash_seed)},
        )

        assert result.stdout.strip() == str(decoy_project / "src")

    def test_excluded_tree_not_walked(self, decoy_project: Path, monkeypatch: pytest.MonkeyPatch):
        """Excluded directories are pruned during the walk, not filtered after it.

        Walking then filtering cost ~17s on a repository holding benchmark snapshots. Assert the traversal itself, since
        a wall-clock assertion would be a flaky proxy for it.
        """
        walked: list[str] = []
        real_walk = _scanner_mod.os.walk

        def _recording_walk(top, *args, **kwargs):
            for dirpath, dirnames, filenames in real_walk(top, *args, **kwargs):
                walked.append(str(dirpath))
                yield dirpath, dirnames, filenames

        monkeypatch.setattr(_scanner_mod.os, "walk", _recording_walk)

        _detect_src_root_from_init(decoy_project)

        # Relative to the fixture: absolute dirpaths carry the tmp prefix, and a dot-component
        # or a "results" ancestor anywhere in it would fail this for reasons unrelated to the walk.
        inside = [Path(p).relative_to(decoy_project).parts for p in walked]
        assert not [parts for parts in inside if "results" in parts]
        assert not [parts for parts in inside if any(part.startswith(".") for part in parts)]

    @pytest.mark.parametrize(
        ("package_dirs", "expected_root"),
        [
            # Dot-directories hold vendored checkouts and caches; they are never part of the import space.
            pytest.param(["src", ".sandbox/vendored/src"], "src", id="dot-directory-cannot-win"),
            # With several non-``src`` candidates the deepest wins; same-depth ties break on the path.
            pytest.param(["alpha", "beta", "gamma/deeper"], "gamma/deeper", id="deepest-non-src-candidate-wins"),
            # The top-level ``src`` wins, not the alphabetically first: a vendored ``a/src`` must lose, ``zz/src`` too.
            pytest.param(["src", "a/src", "zz/src"], "src", id="shallowest-src-wins-regardless-of-alphabet"),
            pytest.param(["b/src", "a/src"], "a/src", id="same-depth-src-candidates-break-ties-on-path"),
            pytest.param(["alpha", "beta"], "beta", id="same-depth-non-src-candidates-break-ties-on-path"),
        ],
    )
    def test_candidate_selection_is_deterministic(self, tmp_path: Path, package_dirs: list[str], expected_root: str):
        """The detected source root is a pure function of the tree, never of hash or filesystem order.

        A ``src`` directory beats any other candidate and the shallowest ``src`` wins over a vendored one; otherwise the
        deepest candidate wins. Equal-depth candidates are ordered by path — ``max`` keyed on depth alone had left them
        to set iteration order, which is exactly the ordering the tie-break exists to pin.
        """
        _write_pkgs(tmp_path, package_dirs)

        assert _detect_src_root_from_init(tmp_path) == tmp_path / expected_root


def test_detect_src_root_prefers_explicit_config_over_walk(tmp_path: Path):
    """An explicit setuptools ``where`` setting still short-circuits the directory walk.

    Both ``src`` and ``lib`` look like source roots, but the project declares ``lib`` in ``pyproject.toml``; pruning
    must not have reordered the strategies so that the walk's pick overrides the user's explicit configuration.
    """
    _write_pkgs(tmp_path, ["src", "lib"])
    (tmp_path / "pyproject.toml").write_text('[tool.setuptools.packages.find]\nwhere = ["lib"]\n', encoding="utf-8")

    assert detect_src_root(tmp_path) == tmp_path / "lib"
