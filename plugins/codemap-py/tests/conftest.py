"""Shared fixtures and helpers for codemap bin integration tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

_BIN_DIR = Path(__file__).parent.parent / "bin"
# bin/ on sys.path so test files can import bin/ Python modules directly.
if str(_BIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_DIR))

_SRC_DIR = Path(__file__).parent.parent / "src"
# src/ on sys.path so `from codemap_py import ...` collects under isolated runs
# (the full suite gets src/ via ``--doctest-modules``; a lone gate run would not).
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

_DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture(name="corpus_dir", scope="session")
def _corpus_dir() -> Path:
    """Path to the frozen-grammar corpus fixture (``tests/data/corpus``)."""
    return _DATA_DIR / "corpus"


@pytest.fixture(name="corpus_pyi_dir", scope="session")
def _corpus_pyi_dir() -> Path:
    """Path to the ``.pyi`` scope-extension fixture project root (``tests/data/corpus_pyi/proj``)."""
    return _DATA_DIR / "corpus_pyi" / "proj"


@pytest.fixture(autouse=True)
def _telemetry_off(monkeypatch):
    """Keep the suite out of the real telemetry stream.

    The 2026-07 usage audit found 4.5K pytest-run records in the dev repo's live cli.jsonl (fixture errors, 30–126s
    suite timings) drowning organic usage. Tests exercising the logging path re-enable it explicitly
    (``monkeypatch.delenv``/``setenv`` as in test_telemetry.py) or pass their own ``log_dir``/``CODEMAP_LOG_DIR``.
    """
    monkeypatch.setenv("CODEMAP_LOGGING", "false")


@pytest.fixture(autouse=True)
def _host_session_off(monkeypatch):
    """Keep the host's own session identity out of the suite.

    ``_hookutil.runtime_session`` and ``codemap_py.telemetry`` prefer ``CLAUDE_CODE_SESSION_ID``/``CSID`` (Claude) and
    ``CODEX_THREAD_ID`` (Codex) over the seeded marker, so a suite run from inside either host's session made every
    seeded-shard join test fail on the host's real session id. Subprocess-spawning tests copy ``os.environ`` and inherit
    the cleared state. Tests that exercise an env fallback set the variable explicitly.
    """
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CSID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)


@pytest.fixture(name="scan_index", scope="session")
def _scan_index() -> Path:
    """Path to the scan-index bin script."""
    return _BIN_DIR / "scan-index"


@pytest.fixture(name="scan_query", scope="session")
def _scan_query() -> Path:
    """Path to the scan-query bin script."""
    return _BIN_DIR / "scan-query"


@pytest.fixture(name="gamma_src", scope="session")
def _gamma_src() -> str:
    """Source for gamma module — leaf, no imports."""
    return "def func_gamma(x):\n    return x + 1\n"


@pytest.fixture(name="beta_src", scope="session")
def _beta_src() -> str:
    """Source for beta module — imports gamma."""
    return "import gamma\n\ndef func_beta(x):\n    return gamma.func_gamma(x) * 2\n"


@pytest.fixture(name="alpha_src", scope="session")
def _alpha_src() -> str:
    """Source for alpha module — imports beta and gamma."""
    return "import beta\nimport gamma\n\ndef func_alpha(x):\n    return beta.func_beta(x) + gamma.func_gamma(x)\n"


@pytest.fixture(name="delta_src", scope="session")
def _delta_src() -> str:
    """Source for delta module — imports alpha."""
    return "import alpha\n\ndef func_delta(x):\n    return alpha.func_alpha(x)\n"


@pytest.fixture(name="project", scope="module")
def _project(tmp_path_factory, gamma_src, beta_src, alpha_src, delta_src, scan_index):
    """Build fixture project, scan once, return (root, index_path)."""
    root = tmp_path_factory.mktemp("proj")
    (root / "gamma.py").write_text(gamma_src)
    (root / "beta.py").write_text(beta_src)
    (root / "alpha.py").write_text(alpha_src)
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "delta.py").write_text(delta_src)

    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root)],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr

    index_path = root / ".cache" / "codemap" / f"{root.name}.json"
    assert index_path.exists(), "scan-index did not produce index file"
    return root, index_path


@pytest.fixture(name="query", scope="module")
def _query(project, scan_query) -> Callable[..., dict]:
    """Return callable that runs scan-query against the module-scoped project."""
    root, index_path = project

    def _query(*args: str) -> dict:
        """Run the fixed query subprocess and decode its JSON response."""
        result = subprocess.run(
            [sys.executable, str(scan_query), "--index", str(index_path), *args],
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        assert result.returncode == 0, result.stderr + result.stdout
        return json.loads(result.stdout)

    return _query


# ── shared polluted-repo fixture for integration-level acceptance ──────
#
# A single realistic tree exercising the index-hardening fixes together, rather
# than the per-concern inline trees used by the unit-level suites. It is a real git
# repo (the staleness diff and self-heal are git-blob based) built by a factory so
# individual tests can toggle a colliding copy, mutate files, and re-scan without
# cross-test contamination.
#
# Layout (dotted names in parentheses), collision-free variant:
#     pkg/__init__.py           (pkg)
#     pkg/leaf.py               (pkg.leaf)     — no imports
#     pkg/consumer.py           (pkg.consumer) — imports pkg.leaf
#     pkg/broken.py             (pkg.broken)   — SyntaxError → degraded module
#     .claude/worktrees/agent-x/pkg/…          — EXCLUDED ghost copy: pruned by 1.2
#     vendored-lib/vendored.py                 — pruned via .codemapignore
#     .codemapignore            — "vendored-lib"
#
# With ``with_collision=True`` an additional non-excluded whole-tree copy is added:
#     wt/pkg/__init__.py, wt/pkg/leaf.py       — top-level package copy → its
#                                                 qualnames collide with pkg/ (1.3)
#
# Why the toggle rather than one always-polluted tree: a recorded collision is an
# index-level blind spot that poisons ``query_complete`` for EVERY command (by
# design — _query_complete gates on collision_count). So the direction-scoped
# completeness and self-heal acceptance (1.1) must run on a collision-free tree,
# while dedup determinism (1.3) needs the colliding copy. The ghost (.claude) and
# the collision copy (wt/) split the dual path the 1.3 implementer called out:
# exclusions prune the ghost before dedup, while wt/ is a genuine non-excluded
# duplicate that produces a real qualname collision.

_PKG_INIT = ""
_LEAF_SRC = "def leaf_fn(x):\n    return x\n"
_CONSUMER_SRC = "import pkg.leaf\n\n\ndef use(x):\n    return pkg.leaf.leaf_fn(x)\n"
_BROKEN_SRC = "def oops(:\n    return\n"  # SyntaxError → degraded
_VENDORED_SRC = "def vendored_fn():\n    return 0\n"


def _git(root: Path, *args: str) -> None:
    """Run a git command inside *root*, asserting success."""
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _index_path_for(root: Path) -> Path:
    """Return the codemap index path scan-index writes for *root*."""
    return root / ".cache" / "codemap" / f"{root.name}.json"


def _run_scan(scan_index: Path, root: Path, *extra: str) -> None:
    """Run scan-index over *root*, asserting success."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr


def _materialize_polluted_tree(root: Path, *, with_collision: bool) -> None:
    """Write the polluted-repo directory tree under *root* (no scan, no git).

    Args:
        root: repo root to populate.
        with_collision: also add the non-excluded ``wt/pkg`` copy so its qualnames
            collide with the canonical ``pkg`` tree.
    """
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(_PKG_INIT)
    (pkg / "leaf.py").write_text(_LEAF_SRC)
    (pkg / "consumer.py").write_text(_CONSUMER_SRC)
    (pkg / "broken.py").write_text(_BROKEN_SRC)

    ghost = root / ".claude" / "worktrees" / "agent-x" / "pkg"
    ghost.mkdir(parents=True)
    (ghost / "__init__.py").write_text(_PKG_INIT)
    (ghost / "leaf.py").write_text(_LEAF_SRC)

    vendored = root / "vendored-lib"
    vendored.mkdir()
    (vendored / "vendored.py").write_text(_VENDORED_SRC)

    (root / ".codemapignore").write_text("# vendored third-party copy\nvendored-lib\n")

    if with_collision:
        wt_pkg = root / "wt" / "pkg"
        wt_pkg.mkdir(parents=True)
        (wt_pkg / "__init__.py").write_text(_PKG_INIT)
        (wt_pkg / "leaf.py").write_text(_LEAF_SRC)


@pytest.fixture(name="polluted_repo")
def _polluted_repo(tmp_path: Path, scan_index: Path) -> Callable[..., tuple[Path, Path]]:
    """Provide a factory that builds and scans a polluted repository.

    Call the returned callable once per test. It initialises a git repo, materialises
    the tree, commits it, runs an initial full scan, and returns the project root and
    index path. Tests may then mutate files under the root and re-scan (or rely on
    scan-query self-heal).

    The callable accepts ``with_collision`` (default ``False``): when ``True`` the
    non-excluded ``wt/pkg`` duplicate is added so the index records a real qualname
    collision. Leave it ``False`` for direction-scoped completeness and
    self-heal tests, which need a collision-free index to reach completeness.

    Returns:
        A factory ``(with_collision=False) -> (root, index_path)``.
    """

    def _build(*, with_collision: bool = False) -> tuple[Path, Path]:
        """Build and scan one isolated polluted-repository variant."""
        root = tmp_path / "polluted"
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@t.t")
        _git(root, "config", "user.name", "t")
        _materialize_polluted_tree(root, with_collision=with_collision)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "init polluted repo")
        _run_scan(scan_index, root)
        index_path = _index_path_for(root)
        assert index_path.exists(), "scan-index did not produce an index file"
        return root, index_path

    return _build
