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
import textwrap
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import codemap_py.scanner as _scanner_mod  # noqa: E402  (needs the sys.path insert above)

_detect_src_root_from_init = _scanner_mod._detect_src_root_from_init
detect_src_root = _scanner_mod.detect_src_root


def _write_pkg(base: Path) -> None:
    """Create a minimal ``pkg`` package under *base* so *base* reads as a source root."""
    pkg = base / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")


@pytest.fixture(name="decoy_project")
def _decoy_project(tmp_path: Path) -> Path:
    """Build a project whose only legitimate source root competes with two excluded decoys."""
    _write_pkg(tmp_path / "src")
    _write_pkg(tmp_path / "vendored" / "results" / "proj" / "src")
    _write_pkg(tmp_path / ".cache" / "proj" / "src")
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [tool.codemap]
            exclude = [
              "results",
            ]
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_excluded_dir_name_cannot_win_detection(decoy_project: Path) -> None:
    """A subtree named in ``[tool.codemap] exclude`` is not a source-root candidate."""
    assert _detect_src_root_from_init(decoy_project) == decoy_project / "src"


def test_codemapignore_is_honoured(tmp_path: Path) -> None:
    """``.codemapignore`` entries exclude candidates, same as the pyproject key."""
    _write_pkg(tmp_path / "src")
    _write_pkg(tmp_path / "fixtures" / "snapshot" / "src")
    (tmp_path / ".codemapignore").write_text("snapshot\n", encoding="utf-8")

    assert _detect_src_root_from_init(tmp_path) == tmp_path / "src"


def test_detection_is_independent_of_hash_seed(decoy_project: Path) -> None:
    """The winner must not vary with ``PYTHONHASHSEED``.

    Candidates were collected into a ``set`` and read back by iteration order, so the detected root changed between runs
    of the same unchanged tree — and with it every module name derived from it. Run in subprocesses because a seed only
    takes effect at interpreter start.
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
    results = {
        subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            # Inherit the environment: replacing it wholesale drops SystemRoot/COMSPEC/
            # PATHEXT/TEMP, and the win32 child then aborts before running with
            # "_Py_HashRandomization_Init: failed to get random numbers".
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
        ).stdout.strip()
        for seed in (0, 1, 2, 3)
    }

    assert results == {str(decoy_project / "src")}


def test_detection_skips_excluded_tree_entirely(decoy_project: Path, monkeypatch) -> None:
    """Excluded directories are pruned during the walk, not filtered after it.

    Walking then filtering cost ~17s on a repository holding benchmark snapshots. Assert the traversal itself, since a
    wall-clock assertion would be a flaky proxy for it.
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


def test_detect_src_root_prefers_explicit_config_over_walk(tmp_path: Path) -> None:
    """Strategy 1 still short-circuits the walk; pruning did not reorder the strategies."""
    _write_pkg(tmp_path / "src")
    _write_pkg(tmp_path / "lib")
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [tool.setuptools.packages.find]
            where = ["lib"]
            """
        ),
        encoding="utf-8",
    )

    assert detect_src_root(tmp_path) == tmp_path / "lib"


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
def test_candidate_selection_is_deterministic(tmp_path: Path, package_dirs: list[str], expected_root: str) -> None:
    """The detected source root is a pure function of the tree, never of hash or filesystem order.

    A ``src`` directory beats any other candidate and the shallowest ``src`` wins over a vendored one; otherwise the
    deepest candidate wins. Equal-depth candidates are ordered by path — ``max`` keyed on depth alone had left them to
    set iteration order, which is exactly the ordering the tie-break exists to pin.
    """
    for package_dir in package_dirs:
        _write_pkg(tmp_path / package_dir)

    assert _detect_src_root_from_init(tmp_path) == tmp_path / expected_root
