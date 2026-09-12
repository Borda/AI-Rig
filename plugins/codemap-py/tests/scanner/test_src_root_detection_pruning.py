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


def test_dot_directory_cannot_win_detection(tmp_path: Path) -> None:
    """A dot-directory is never part of the import space, so it cannot be the source root.

    Dot-directories hold vendored checkouts and caches. The detector prunes them the way
    :func:`codemap_py.scanner.is_excluded` already did for every other walk.
    """
    _write_pkg(tmp_path / "src")
    _write_pkg(tmp_path / ".sandbox" / "vendored" / "src")

    assert _detect_src_root_from_init(tmp_path) == tmp_path / "src"


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
        subprocess.run(  # noqa: S603 - fixed argv, no shell
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


def test_multiple_candidates_resolve_deterministically(tmp_path: Path) -> None:
    """With several non-``src`` candidates the deepest wins, ties broken on the path.

    ``max`` over a set keyed on depth alone left same-depth candidates to hash order.
    """
    _write_pkg(tmp_path / "alpha")
    _write_pkg(tmp_path / "beta")
    _write_pkg(tmp_path / "gamma" / "deeper")

    assert _detect_src_root_from_init(tmp_path) == tmp_path / "gamma" / "deeper"


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


def test_shallowest_src_wins_regardless_of_alphabet(tmp_path: Path) -> None:
    """With several ``src`` directories the top-level one wins, not the alphabetically first.

    Selecting the sorted-first candidate is deterministic but arbitrary: it made a vendored
    ``a/src`` beat the real top-level ``src`` while ``zz/src`` lost to it. Both orderings
    are asserted so a regression cannot hide behind a lucky fixture name.
    """
    _write_pkg(tmp_path / "src")
    _write_pkg(tmp_path / "a" / "src")
    _write_pkg(tmp_path / "zz" / "src")

    assert _detect_src_root_from_init(tmp_path) == tmp_path / "src"


def test_same_depth_src_candidates_break_ties_on_path(tmp_path: Path) -> None:
    """Equally shallow ``src`` candidates resolve to the same one on every run."""
    _write_pkg(tmp_path / "b" / "src")
    _write_pkg(tmp_path / "a" / "src")

    assert _detect_src_root_from_init(tmp_path) == tmp_path / "a" / "src"


def test_same_depth_non_src_candidates_break_ties_on_path(tmp_path: Path) -> None:
    """The depth fallback is total: equal-depth candidates are ordered by path, not by hash.

    ``max`` keyed on depth alone left same-depth candidates to set iteration order, which is the ordering the tie-break
    exists to pin.
    """
    _write_pkg(tmp_path / "alpha")
    _write_pkg(tmp_path / "beta")

    assert _detect_src_root_from_init(tmp_path) == tmp_path / "beta"
