"""Shared fixtures for benchmarks test suite.

Generated-manifest session hooks live in the parent ``benchmarks/conftest.py``: only conftests for the ``testpaths``
collection roots load before ``pytest_sessionstart``, so hooks placed here never ran on a fresh clone.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

BENCHMARKS_DIR = Path(__file__).parent.parent
REPO_ROOT = BENCHMARKS_DIR.parent
TESTS_DIR = Path(__file__).parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
PYTORCH_LIGHTNING_REPO = Path(os.environ.get("PL_REPO_PATH", str(REPO_ROOT / ".sandbox" / "pytorch-lightning")))
PYTORCH_LIGHTNING_INDEXES = (
    tuple(sorted(PYTORCH_LIGHTNING_REPO.rglob(".cache/codemap/*.json"))) if PYTORCH_LIGHTNING_REPO.exists() else ()
)


def _load_module(module_name: str, filename: str):
    """Load a benchmarks script by filename via importlib.

    Args:
        module_name: Name to register in sys.modules (must be a valid identifier).
        filename: Filename of the script relative to BENCHMARKS_DIR.

    Returns:
        The loaded module with all public symbols accessible.

    Example:
        >>> module = _load_module("example_python_source", "_bench_common/python_source.py")
        >>> module.__name__, sys.modules["example_python_source"] is module
        ('example_python_source', True)
        >>> _ = sys.modules.pop("example_python_source")
    """
    # Runners import private benchmark packages from their own directory; add it
    # because spec_from_file_location does not set it as sys.path[0].
    if str(BENCHMARKS_DIR) not in sys.path:
        sys.path.insert(0, str(BENCHMARKS_DIR))
    path = BENCHMARKS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(name="script_claude_stream", scope="session")
def _script_claude_stream():
    """Cache the shared Claude transport module for the test session without invoking a provider.

    >>> getfixture("script_claude_stream").__name__
    'benchmarks_claude_transport'
    """
    return _load_module("benchmarks_claude_transport", "_bench_common/claude_transport.py")


@pytest.fixture(name="script_python_source", scope="session")
def _script_python_source():
    """Cache the source-analysis module for tests that inspect synthetic Python import graphs.

    >>> getfixture("script_python_source").__name__
    'benchmarks_python_source'
    """
    return _load_module("benchmarks_python_source", "_bench_common/python_source.py")


@pytest.fixture(name="script_benchmark_paths", scope="session")
def _script_benchmark_paths():
    """Cache benchmark path and metadata helpers without loading a provider runner.

    >>> getfixture("script_benchmark_paths").__name__
    'benchmarks_benchmark_paths'
    """
    return _load_module("benchmarks_benchmark_paths", "_bench_common/benchmark_paths.py")


@pytest.fixture(name="script_run_agentic", scope="session")
def _script_run_agentic():
    """Cache the agentic runner's definitions without entering its command-line entry point.

    >>> getfixture("script_run_agentic").__name__
    'run_claude_agentic'
    """
    return _load_module("run_claude_agentic", "run-claude-agentic.py")


@pytest.fixture(name="script_run_codex", scope="session")
def _script_run_codex():
    """Cache the Codex structural adapter's definitions without entering its command-line entry point.

    >>> getfixture("script_run_codex").__name__
    'run_codemap_codex'
    """
    return _load_module("run_codemap_codex", "run-codex-structural.py")


@pytest.fixture(name="script_run_bench", scope="session")
def _script_run_bench():
    """Cache the structural runner's definitions without executing a benchmark or provider call.

    >>> getfixture("script_run_bench").__name__
    'run_claude_structural'
    """
    return _load_module("run_claude_structural", "run-claude-structural.py")


@pytest.fixture(name="script_run_cli", scope="session")
def _script_run_cli():
    """Cache the CLI benchmark module without launching its subprocess benchmarks.

    >>> getfixture("script_run_cli").__name__
    'run_cli'
    """
    return _load_module("run_cli", "run-codemap-cli.py")


@pytest.fixture(name="script_gen_bench", scope="session")
def _script_gen_bench():
    """Cache the benchmark-task generator without writing generated suites.

    >>> getfixture("script_gen_bench").__name__
    'generate_tasks_bench'
    """
    return _load_module("generate_tasks_bench", "generate-tasks-bench.py")


@pytest.fixture(name="script_gen_real_issues", scope="session")
def _script_gen_real_issues():
    """Cache the real-issue generator without fetching GitHub data or generating tasks.

    >>> getfixture("script_gen_real_issues").__name__
    'generate_tasks_real_issues'
    """
    return _load_module("generate_tasks_real_issues", "generate-tasks-real-issues.py")


# ---------------------------------------------------------------------------
# Integration fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="scan_query_binary", scope="session")
def _scan_query_binary() -> Path:
    """Path to scan-query binary; fails on POSIX when the tracked binary is absent.

    The enclosing tests use a collection-time launchability marker. Once selected,
    a missing tracked binary means a broken checkout, so fail loudly rather than
    yielding a false-green skip.

    Returns:
        Absolute path to the scan-query executable.

    Example:
        >>> getfixture("scan_query_binary").name
        'scan-query'
    """
    binary = REPO_ROOT / "plugins" / "codemap-py" / "bin" / "scan-query"
    if not binary.exists():
        pytest.fail(f"tracked scan-query binary missing at {binary} — broken checkout")
    return binary


@pytest.fixture(name="scan_index_binary", scope="session")
def _scan_index_binary() -> Path:
    """Path to scan-index binary; fails on POSIX when the tracked binary is absent.

    The enclosing tests use a collection-time launchability marker. Once selected,
    a missing tracked binary means a broken checkout, so fail loudly rather than
    yielding a false-green skip.

    Returns:
        Absolute path to the scan-index executable.

    Example:
        >>> getfixture("scan_index_binary").name
        'scan-index'
    """
    binary = REPO_ROOT / "plugins" / "codemap-py" / "bin" / "scan-index"
    if not binary.exists():
        pytest.fail(f"tracked scan-index binary missing at {binary} — broken checkout")
    return binary


@pytest.fixture(name="sample_repo", scope="session")
def _sample_repo(tmp_path_factory: pytest.TempPathFactory, scan_index_binary: Path) -> tuple[Path, Path]:
    """Clone psf/requests (shallow) and build a codemap index.

    Fails if clone or indexing fails after collection.

    Args:
        tmp_path_factory: pytest factory for session-scoped temp directories.
        scan_index_binary: Path to the scan-index executable.

    Returns:
        Tuple of (repo_path, index_path) for use in integration tests.
    """
    clone_dir = tmp_path_factory.mktemp("sample-repo") / "requests"
    try:
        subprocess.run(
            ["git", "clone", "--depth=1", "https://github.com/psf/requests.git", str(clone_dir)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"git clone failed (no network?): {exc}")

    try:
        subprocess.run(
            [str(scan_index_binary), "--root", str(clone_dir)],
            check=True,
            capture_output=True,
            timeout=120,
            cwd=str(clone_dir),
        )
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"scan-index failed: {exc.stderr.decode()}")

    index_candidates = sorted(clone_dir.rglob(".cache/codemap/*.json"))
    if not index_candidates:
        pytest.fail("scan-index ran but produced no index file")

    return clone_dir, index_candidates[0]


@pytest.fixture(name="pytorch_lightning_repo", scope="session")
def _pytorch_lightning_repo() -> Path:
    """Path to a decorator-validated pytorch-lightning checkout.

    Checks ``PL_REPO_PATH`` env var first, then the pinned in-project clone
    ``.sandbox/pytorch-lightning`` (created by ``run-all.sh``).

    Returns:
        Absolute path to the pytorch-lightning repository root.
    """
    assert PYTORCH_LIGHTNING_REPO.is_dir(), "pytorch-lightning test lacks checkout marker"
    return PYTORCH_LIGHTNING_REPO


@pytest.fixture(name="pytorch_lightning_index", scope="session")
def _pytorch_lightning_index(pytorch_lightning_repo: Path, scan_query_binary: Path) -> Path:
    """Decorator-validated pre-built Codemap index for pytorch-lightning.

    The index is expected at ``.cache/codemap/<repo-name>.json`` inside the
    repo.  Run ``scan-index --root <repo>`` once to create it.

    Args:
        pytorch_lightning_repo: Root of the pytorch-lightning checkout.
        scan_query_binary: Ensures the binary fixture was resolved first.

    Returns:
        Absolute path to the JSON index file.
    """
    _ = scan_query_binary  # ensure binary fixture resolved
    assert PYTORCH_LIGHTNING_INDEXES, "pytorch-lightning index test lacks index marker"
    return PYTORCH_LIGHTNING_INDEXES[0]
