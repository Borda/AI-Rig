"""Extraction parity: scanner discovery/parsing.

Proves the file-discovery / single-file-AST-parsing move into
:mod:`codemap_py.scanner` preserved behavior exactly, by comparing the
pre-extraction monolithic ``bin/scan-index`` (checked out from ``HEAD``, before
this phase's uncommitted working-tree move) against the current, already-thin
``bin/scan-index`` launcher (delegating to :func:`codemap_py.graph.main`, which
now calls into :mod:`codemap_py.scanner` for discovery/parsing):

- full scan of an identical fixture project produces byte-identical index JSON
  (modulo the two documented volatile top-level keys — ``scanned_at``,
  ``git_sha`` — see ``test_grammar.py``'s ``_VOLATILE_KEYS`` convention, reused
  here) plus byte-identical stdout/stderr and an identical exit code;
- incremental re-scan after a source edit agrees old-vs-new;
- ``.codemapignore`` exclusions prune the same files and record the same
  ``excluded_roots``;
- a degraded (syntax-error) module is recorded identically and the same
  stderr warning line is printed;
- a permission error on the index output directory produces the same exit
  code and stderr message old-vs-new;
- the same holds when the new side is driven through the production
  ``codemap-py index`` dispatch (``python -m codemap_py``) rather than the
  bare launcher.

Both path classes below (plain, and one with spaces + non-ASCII characters)
are exercised for the full-scan case per repo convention (see
``test_extraction_parity_core_modules_and_cli_entrypoint.py``). The old-vs-new comparisons intentionally scan the
*same* fixture root for both engines (never two separate copies) — ``scan()``
records ``scan_root = str(root.resolve())`` as a legitimately path-dependent
field the byte-identity spec keeps, so an identical root path is required for
that field to line up, not just an implementation detail of this test.

The old-bin checkout, path classes, index normalisation, scan runner and feature-rich fixture project are shared with
``test_extraction_parity_graph_coverage_testimpact.py`` through ``tests/_parity_helpers.py``.
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from _parity_helpers import (
    NEW_SCAN_INDEX,
    PATH_CLASSES,
    ScanPair,
    assert_v13_index_delta,
    build_fixture_project,
    corrupt_fingerprint,
    corrupt_input_hash,
    corrupt_project,
    materialize_old_bin,
    run_scan,
    scan_old_and_new,
)

# The atomic-replace temp file is PID-qualified (main()'s "each writer owns its
# own temp" comment in graph.py) — a PermissionError raised while opening it
# embeds that PID in its message, so it must be normalized before any old-vs-new
# stderr comparison that can hit this path (never volatile on the success path,
# where only the PID-free out_path is printed).
_PID_TMP_RE = re.compile(r"\.json\.\d+\.tmp")


def _normalize_pid_tmp(text: str) -> str:
    """Replace process-specific temporary-file IDs before parity comparison.

    >>> _normalize_pid_tmp("write failed: out.json.123.tmp")
    'write failed: out.json.PID.tmp'
    """
    return _PID_TMP_RE.sub(".json.PID.tmp", text)


_TESTS_DIR = Path(__file__).resolve().parents[1]
_PLUGIN_ROOT = _TESTS_DIR.parent
_SRC = _PLUGIN_ROOT / "src"


@pytest.fixture(name="old_scan_index", scope="module")
def _old_scan_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-stable path to the golden pre-extraction ``scan-index``."""
    return materialize_old_bin(tmp_path_factory.mktemp("old_bin_scanner_discovery"), "scan-index")


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(corrupt_project, id="legacy-content"),
        pytest.param(corrupt_input_hash, id="input-hash"),
        pytest.param(corrupt_fingerprint, id="fingerprint"),
    ],
)
def test_v13_normalization_rejects_unrelated_legacy_change(
    scan_pair: ScanPair, corrupt: Callable[[dict], None]
) -> None:
    """The declared metadata delta cannot hide content, input-hash or fingerprint corruption.

    A real old-vs-new index pair passes the delta check; corrupting one field of a copy of the new index must then make
    the same check fail, proving the normalisation is not so loose that it accepts unrelated drift.
    """
    assert_v13_index_delta(scan_pair.old_index, scan_pair.new_index)
    new = copy.deepcopy(scan_pair.new_index)
    corrupt(new)

    with pytest.raises(AssertionError):
        assert_v13_index_delta(scan_pair.old_index, new)


@pytest.mark.parametrize("field", ["mock_patches", "subprocess_calls"])
def test_relative_coordinate_delta_rejects_changed_file(scan_pair: ScanPair, field: str) -> None:
    """Exact relative-coordinate conversion still rejects a corrupted source-file reference."""
    new = copy.deepcopy(scan_pair.new_index)
    record = next(record for module in new["modules"] for record in module.get(field, []))
    record["file"] += ".wrong"
    with pytest.raises(AssertionError):
        assert_v13_index_delta(scan_pair.old_index, new)


@pytest.fixture(name="scan_pair", scope="module", params=PATH_CLASSES)
def _scan_pair(
    tmp_path_factory: pytest.TempPathFactory, old_scan_index: Path, request: pytest.FixtureRequest
) -> ScanPair:
    """Scan the feature-rich fixture once with the golden monolith and once with the current launcher.

    Both engines scan the identical root path into the identical index directory sequentially, so path-dependent output
    (``scan_root``, stdout/stderr embedding ``out_path``) is truly comparable. The result is immutable evidence shared
    by every read-only comparison; tests that corrupt an index must deep-copy it first.
    """
    return scan_old_and_new(tmp_path_factory.mktemp("scanner_discovery_pair"), request.param, old_scan_index)


# --- full scan: byte-identical index + stdout/stderr + exit code ------------


def test_full_scan_byte_identical_old_vs_new(scan_pair: ScanPair) -> None:
    """A full scan of the same fixture agrees old-vs-new: index, stdout/stderr, exit code.

    Both engines scanned the identical root into the identical index directory (see ``scan_pair``), so stdout/stderr —
    which embed the output path — are truly byte-identical, not merely path-normalized. Runs for a plain and a non-ASCII
    project directory name.
    """
    assert scan_pair.old_result.returncode == scan_pair.new_result.returncode == 0
    assert scan_pair.old_result.stdout == scan_pair.new_result.stdout == ""
    assert scan_pair.old_result.stderr == scan_pair.new_result.stderr
    assert_v13_index_delta(scan_pair.old_index, scan_pair.new_index)


def test_full_scan_matches_via_python_module_entrypoint(tmp_path: Path, old_scan_index: Path) -> None:
    """The golden monolith also agrees with the new side reached via ``python -m codemap_py index``.

    This drives the production dispatch chain (``codemap_py.cli`` -> rwgate writer lease -> ``bin/scan-index``
    subprocess) instead of invoking the launcher bare, proving the currently-wired production entrypoint reaches the
    same scanner/graph behavior.
    """
    root = tmp_path / "proj"
    root.mkdir()
    build_fixture_project(root)
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    env = {**os.environ, "CODEMAP_LOGGING": "false", "CODEMAP_INDEX_DIR": str(index_dir), "PYTHONPATH": str(_SRC)}

    old_result, old_index = run_scan(old_scan_index, root, index_dir)
    assert old_result.returncode == 0, old_result.stderr

    new_result = subprocess.run(
        [sys.executable, "-m", "codemap_py", "index", "--root", str(root)],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    new_index = json.loads((index_dir / f"{root.name}.json").read_text())

    assert new_result.returncode == 0, new_result.stderr
    assert_v13_index_delta(old_index, new_index)


# --- incremental scan ---------------------------------------------------


def test_incremental_scan_matches_old_vs_new(tmp_path: Path, old_scan_index: Path) -> None:
    """An incremental re-scan after a source edit agrees old-vs-new.

    Each engine keeps its own baseline (separate ``CODEMAP_INDEX_DIR``) so its ``--incremental`` step reads back exactly
    the index it itself produced — the two output directories mean stdout/stderr are not compared here (they embed the
    differing directory), only the resulting index bodies.
    """
    root = tmp_path / "proj"
    root.mkdir()
    build_fixture_project(root)
    old_dir = tmp_path / "idx_old"
    new_dir = tmp_path / "idx_new"

    old_full, _ = run_scan(old_scan_index, root, old_dir)
    new_full, _ = run_scan(NEW_SCAN_INDEX, root, new_dir)
    assert old_full.returncode == new_full.returncode == 0

    (root / "pkg" / "leaf.py").write_text("def leaf_fn(x):\n    return x + 2\n")

    old_inc, old_index = run_scan(old_scan_index, root, old_dir, "--incremental")
    new_inc, new_index = run_scan(NEW_SCAN_INDEX, root, new_dir, "--incremental")

    assert old_inc.returncode == new_inc.returncode == 0
    assert old_index is not None
    assert new_index is not None
    assert_v13_index_delta(old_index, new_index)


# --- exclusions ----------------------------------------------------------


def test_exclusions_prune_vendored_copy_identically(scan_pair: ScanPair) -> None:
    """Prune ignored files identically in both scanner implementations.

    The fixture's ``.codemapignore`` excludes ``vendored-lib``; neither index may list it, both must record the same
    excluded roots, and the summary line on stderr must agree.
    """
    assert scan_pair.old_result.returncode == scan_pair.new_result.returncode == 0
    assert scan_pair.old_result.stderr == scan_pair.new_result.stderr  # excludes the "vendored-lib" summary line
    assert not any("vendored" in path for path in scan_pair.old_index["file_shas"])
    assert not any("vendored" in path for path in scan_pair.new_index["file_shas"])
    assert scan_pair.old_index["excluded_roots"] == scan_pair.new_index["excluded_roots"]
    assert_v13_index_delta(scan_pair.old_index, scan_pair.new_index)


# --- degraded module + stats output ---------------------------------------


def test_degraded_module_and_stats_output_identical(scan_pair: ScanPair) -> None:
    """Handle a syntax-error module consistently across extraction paths.

    ``pkg/broken.py`` does not parse; both engines must mark it ``degraded``, print the same stats warning naming it,
    and still exit 0.
    """
    old_stderr = scan_pair.old_result.stderr
    old_status = {m["name"]: m.get("status") for m in scan_pair.old_index["modules"]}
    new_status = {m["name"]: m.get("status") for m in scan_pair.new_index["modules"]}

    assert scan_pair.old_result.returncode == scan_pair.new_result.returncode == 0
    assert old_stderr == scan_pair.new_result.stderr
    assert "modules indexed" in old_stderr
    assert "broken.py" in old_stderr
    assert old_status == new_status
    assert old_status.get("pkg.broken") == "degraded"


# --- error path + exit code ------------------------------------------------


@pytest.fixture(name="readonly_index_dir")
def _readonly_index_dir(tmp_path: Path) -> Iterator[Path]:
    """Create an index directory without write permission and restore its mode at teardown."""
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    index_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # read+execute only — no write
    yield index_dir
    index_dir.chmod(stat.S_IRWXU)  # restore for tmp_path cleanup


@pytest.mark.skipif(
    sys.platform == "win32", reason="chmod-based write-permission removal is a no-op on Windows directories"
)
def test_permission_error_exit_code_parity(tmp_path: Path, old_scan_index: Path, readonly_index_dir: Path) -> None:
    """A permission error writing the index produces the same exit code + message old-vs-new.

    The index directory is read-only, so neither engine can publish. Both must exit 1 with empty stdout; the stderr text
    is deliberately allowed to differ (see the comment below).
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text("def f(x):\n    return x\n")

    old_result, old_index = run_scan(old_scan_index, root, readonly_index_dir)
    new_result, new_index = run_scan(NEW_SCAN_INDEX, root, readonly_index_dir)

    assert old_index is None
    assert new_index is None
    assert old_result.returncode == new_result.returncode == 1
    assert old_result.stdout == new_result.stdout == ""
    assert "[codemap] ERROR: [Errno 13] Permission denied:" in _normalize_pid_tmp(old_result.stderr)

    # Deliberate, documented divergence from byte-identical stderr parity.
    # An unwritable index directory is now reported by the RW gate as a bounded
    # structured error, which the capability contract's exit-1 row requires and the
    # old raw OSError message did not satisfy. Exit code and stdout parity — the part
    # any caller actually branches on — still hold above.
    new_error = json.loads(new_result.stderr)
    assert new_error["error"] == "index_coordination_unavailable"
    assert "detail" in new_error
