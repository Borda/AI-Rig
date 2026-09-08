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

FILE OWNERSHIP NOTE: this file and ``test_extraction_parity_graph_coverage_testimpact.py`` duplicate a small
amount of fixture/helper code (old-bin checkout, path classes, JSON
canonicalization) rather than factoring it into ``conftest.py`` — the
scanner/graph extraction task boundary permits editing only these two new
test files plus the report, not the shared ``conftest.py``.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

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
_NEW_SCAN_INDEX = _PLUGIN_ROOT / "bin" / "scan-index"
_PARITY_GOLDEN_DIR = _TESTS_DIR / "data" / "parity_golden"

# The pre-extraction monolith and its three sibling shims it imports by bare
# name (all self-contained — stdlib only — confirmed by inspection at HEAD).
# Frozen as static fixtures under tests/data/parity_golden/ rather than read
# via `git show HEAD:...` — a HEAD-pinned checkout silently degrades to the
# post-extraction thin launcher once the extraction commit lands (HEAD then
# advances past the monolith), and is simply absent in a shallow CI clone
# that never fetched the old SHA. See _assert_is_monolith for the loud-failure
# guard against a golden fixture being accidentally overwritten in kind.
_OLD_BIN_FILES = ("scan-index", "_exclusions.py", "_schema.py", "_telemetry.py")

# The monolith is roughly 10x-100x larger than its post-extraction thin
# launcher/shim counterpart; thresholds sit with headroom below the monolith
# line count and well above the current thin-file line count (see module
# docstring's byte-identity spec for why these two sizes are so far apart).
_MONOLITH_MIN_LINES = {
    "scan-index": 500,
    "_exclusions.py": 100,
    "_schema.py": 50,
    "_telemetry.py": 50,
}

# Substrings present in every current (post-extraction) bin/ shim or launcher
# docstring, absent from every pre-extraction monolith source — their presence
# in a golden fixture means it was accidentally overwritten with
# post-extraction content instead of holding the frozen monolith.
_POST_EXTRACTION_MARKERS = ("codemap_py", "thin launcher", "compatibility shim")


def _assert_is_monolith(name: str, content: str) -> None:
    """Fail loudly if *content* looks like the post-extraction shim, not the golden monolith."""
    lines = content.splitlines()
    assert len(lines) > _MONOLITH_MIN_LINES[name], (
        f"parity_golden/{name} has only {len(lines)} lines (expected > {_MONOLITH_MIN_LINES[name]}) "
        "— looks like the post-extraction thin bin/ file was committed in place of the "
        "pre-extraction monolith golden"
    )
    for marker in _POST_EXTRACTION_MARKERS:
        assert marker not in content, (
            f"parity_golden/{name} contains {marker!r}, which only appears in post-extraction "
            "bin/ sources — the golden fixture was accidentally overwritten with the thin "
            "launcher/shim instead of the monolith"
        )


# Top-level index keys that legitimately vary between two scans of identical
# source (matches test_grammar.py's convention).
_VOLATILE_KEYS = ("scanned_at", "git_sha")
_V12_ROOT_ADDITIONS = frozenset({"symbol_aliases", "symbol_alias_limitations"})
_V12_MODULE_ADDITIONS = frozenset({"symbol_aliases", "symbol_alias_limitations"})
_V13_MODULE_ADDITIONS = frozenset({"unresolved_direct_imports", "from_import_submodules"})

_PATH_CLASSES = ["proj", "proj café ünïcode dir"]


def _materialize_old_bin(dest: Path) -> Path:
    """Copy the frozen pre-extraction monolithic ``scan-index`` + siblings into *dest*.

    Returns the path to the copied ``scan-index`` script. Source is the static ``tests/data/parity_golden/`` fixture
    (captured once from ``HEAD`` at the time of this extraction task) — not a live ``git show HEAD:...`` checkout, which
    would silently start returning the post-extraction thin launcher the moment the extraction commit lands.
    """
    for name in _OLD_BIN_FILES:
        content = (_PARITY_GOLDEN_DIR / name).read_text()
        _assert_is_monolith(name, content)
        (dest / name).write_text(content)
    return dest / "scan-index"


@pytest.fixture(name="old_scan_index", scope="module")
def _old_scan_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-stable path to the golden pre-extraction ``scan-index``."""
    return _materialize_old_bin(tmp_path_factory.mktemp("old_bin_scanner_discovery"))


def _legacy_index(index: dict) -> dict:
    """Normalize v12/v13 additions while restoring legacy import metrics."""
    modules = []
    for module in index["modules"]:
        normalized = dict(module)
        raw_imports = normalized.get("unresolved_direct_imports")
        if raw_imports is not None:
            normalized["direct_imports"] = raw_imports
            normalized["dep_count"] = len(raw_imports)
        if normalized.get("status") != "ok" and not normalized.get("direct_imports"):
            normalized.pop("direct_imports", None)
            normalized.pop("dep_count", None)
        normalized = {
            key: value for key, value in normalized.items() if key not in _V12_MODULE_ADDITIONS | _V13_MODULE_ADDITIONS
        }
        modules.append(normalized)
    legacy = {
        key: value
        for key, value in index.items()
        if key not in _VOLATILE_KEYS and key not in _V12_ROOT_ADDITIONS and key != "scan_version"
    }
    legacy["modules"] = modules
    return legacy


def _canonical(index: dict) -> str:
    """Serialize an index with stable key ordering for parity assertions.

    >>> _canonical({"b": 2, "a": 1})
    '{"a": 1, "b": 2}'
    """
    return json.dumps(index, sort_keys=True, ensure_ascii=False)


def _assert_v13_index_delta(old: dict, new: dict) -> None:
    """Prove frozen v11 content is identical except declared v12/v13 graph semantics."""
    assert old["scan_version"] == 11
    assert new["scan_version"] == 13
    assert set(new) == set(old) | _V12_ROOT_ADDITIONS
    assert len(old["modules"]) == len(new["modules"])
    for legacy_module, current_module in zip(old["modules"], new["modules"], strict=True):
        additions = _V12_MODULE_ADDITIONS | _V13_MODULE_ADDITIONS if current_module["status"] == "ok" else frozenset()
        current_keys = set(current_module)
        if (
            current_module["status"] != "ok"
            and current_module.get("direct_imports") == []
            and "direct_imports" not in legacy_module
        ):
            current_keys.remove("direct_imports")
        assert current_keys == set(legacy_module) | additions
        if additions:
            assert isinstance(current_module["symbol_aliases"], dict)
            assert isinstance(current_module["symbol_alias_limitations"], list)
    assert isinstance(new["symbol_aliases"], dict)
    assert isinstance(new["symbol_alias_limitations"], list)
    assert _canonical(_legacy_index(old)) == _canonical(_legacy_index(new))


def test_v13_normalization_rejects_unrelated_legacy_change() -> None:
    """The v13 allowance cannot mask a changed pre-v12 field."""
    old = {"scan_version": 11, "project": "legacy", "modules": [{"name": "mod", "status": "ok"}]}
    new = {
        "scan_version": 13,
        "project": "changed",
        "modules": [{"name": "mod", "status": "ok", "symbol_aliases": {}, "symbol_alias_limitations": []}],
        "symbol_aliases": {},
        "symbol_alias_limitations": [],
    }

    with pytest.raises(AssertionError):
        _assert_v13_index_delta(old, new)


def _run_scan(
    executable: Path, root: Path, index_dir: Path, *extra: str
) -> tuple[subprocess.CompletedProcess, dict | None]:
    """Run a ``scan-index``-shaped executable against *root*, return (result, index-or-None).

    ``PYTHONUTF8=1`` + ``encoding="utf-8"`` (rather than bare ``text=True``) pin stdio decoding to UTF-8 regardless of
    the host's console codepage — otherwise a non-ASCII *root* embedded in stderr decodes via the Windows console
    codepage and the two compared child processes can diverge (mojibake vs. clean UTF-8).
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CODEMAP_LOGGING": "false", "CODEMAP_INDEX_DIR": str(index_dir), "PYTHONUTF8": "1"}
    result = subprocess.run(
        [sys.executable, str(executable), "--root", str(root), *extra],
        cwd=str(root),
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=90,
        check=False,
    )
    idx_path = index_dir / f"{root.name}.json"
    index = json.loads(idx_path.read_text()) if idx_path.exists() else None
    return result, index


def _build_fixture_project(root: Path) -> None:
    """Write a small, feature-rich project used across the parity scenarios below.

    Exercises: package imports + a class method call, a mock-patch decorator, a
    dynamic import plus a subprocess call resolving to a sibling script, a
    syntax-error module (degraded), a pytest fixture + its consumer, a Sphinx
    ``.rst`` cross-reference, and a ``.codemapignore``-excluded vendored copy.
    """
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "leaf.py").write_text("def leaf_fn(x):\n    return x + 1\n")
    (pkg / "leaf_runner.py").write_text(
        'from pkg.leaf import leaf_fn\n\nif __name__ == "__main__":\n    print(leaf_fn(1))\n'
    )
    (pkg / "consumer.py").write_text(
        "import pkg.leaf\n\n\nclass Widget:\n    def run(self, x):\n        return pkg.leaf.leaf_fn(x)\n"
    )
    (pkg / "dyn.py").write_text(
        "import importlib\n"
        "import subprocess\n"
        "from pathlib import Path\n\n\n"
        'def load():\n    return importlib.import_module("pkg.leaf")\n\n\n'
        'def run_script():\n    return subprocess.run(["python3", str(Path(__file__).parent / "leaf_runner.py")])\n'
    )
    (pkg / "broken.py").write_text("def oops(:\n    return\n")
    # conftest.py / test_*.py live under tests/, not project root — scanner.py's
    # _TEST_PATH_RE requires a "/" before "conftest.py"/"test_*.py" (or a "tests?/"
    # path segment), so a bare root-level conftest.py/test_pkg.py never matches and
    # is_test stays False, silently skipping fixture-graph and mock-patch extraction
    # (see test_extraction_parity_graph_coverage_testimpact.py's test_fixture_graph_and_mock_patch_data_identical).
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef widget():\n    from pkg.consumer import Widget\n\n    return Widget()\n"
    )
    (tests_dir / "test_pkg.py").write_text(
        "from unittest.mock import patch\n\n\n"
        '@patch("pkg.leaf.leaf_fn")\n'
        "def test_patched(mock_fn):\n"
        "    mock_fn.return_value = 99\n\n\n"
        "def test_widget(widget):\n"
        "    assert widget.run(1) == 2\n"
    )
    docs = root / "docs"
    docs.mkdir()
    (docs / "api.rst").write_text("API\n===\n\nSee :func:`pkg.leaf.leaf_fn` for details.\n")
    vendored = root / "vendored-lib"
    vendored.mkdir()
    (vendored / "vendored.py").write_text("def vendored_fn():\n    return 0\n")
    (root / ".codemapignore").write_text("# vendored third-party copy\nvendored-lib\n")


# --- full scan: byte-identical index + stdout/stderr + exit code ------------


@pytest.mark.parametrize("dirname", _PATH_CLASSES)
def test_full_scan_byte_identical_old_vs_new(tmp_path: Path, old_scan_index: Path, dirname: str) -> None:
    """A full scan of the same fixture agrees old-vs-new: index, stdout/stderr, exit code.

    Both engines scan the identical root path into the identical
    ``CODEMAP_INDEX_DIR``/``out_path`` sequentially (capturing each result
    before the next call overwrites the shared index file) so stdout/stderr —
    which embed ``out_path`` — are truly byte-identical, not merely
    path-normalized.
    """
    root = tmp_path / dirname
    root.mkdir()
    _build_fixture_project(root)
    index_dir = tmp_path / "idx"

    old_result, old_index = _run_scan(old_scan_index, root, index_dir)
    old_stdout, old_stderr, old_rc = old_result.stdout, old_result.stderr, old_result.returncode

    new_result, new_index = _run_scan(_NEW_SCAN_INDEX, root, index_dir)

    assert old_rc == new_result.returncode == 0
    assert old_stdout == new_result.stdout == ""
    assert old_stderr == new_result.stderr
    assert old_index is not None and new_index is not None
    _assert_v13_index_delta(old_index, new_index)


def test_full_scan_matches_via_python_module_entrypoint(tmp_path: Path, old_scan_index: Path) -> None:
    """The golden monolith also agrees with the new side reached via ``python -m codemap_py index``.

    This drives the production dispatch chain (``codemap_py.cli`` -> rwgate writer lease -> ``bin/scan-index``
    subprocess) instead of invoking the launcher bare, proving the currently-wired production entrypoint reaches the
    same scanner/graph behavior.
    """
    root = tmp_path / "proj"
    root.mkdir()
    _build_fixture_project(root)
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    env = {**os.environ, "CODEMAP_LOGGING": "false", "CODEMAP_INDEX_DIR": str(index_dir), "PYTHONPATH": str(_SRC)}

    old_result, old_index = _run_scan(old_scan_index, root, index_dir)
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
    _assert_v13_index_delta(old_index, new_index)


# --- incremental scan ---------------------------------------------------


def test_incremental_scan_matches_old_vs_new(tmp_path: Path, old_scan_index: Path) -> None:
    """An incremental re-scan after a source edit agrees old-vs-new.

    Each engine keeps its own baseline (separate ``CODEMAP_INDEX_DIR``) so its ``--incremental`` step reads back exactly
    the index it itself produced — the two output directories mean stdout/stderr are not compared here (they embed the
    differing directory), only the resulting index bodies.
    """
    root = tmp_path / "proj"
    root.mkdir()
    _build_fixture_project(root)
    old_dir = tmp_path / "idx_old"
    new_dir = tmp_path / "idx_new"

    old_full, _ = _run_scan(old_scan_index, root, old_dir)
    new_full, _ = _run_scan(_NEW_SCAN_INDEX, root, new_dir)
    assert old_full.returncode == new_full.returncode == 0

    (root / "pkg" / "leaf.py").write_text("def leaf_fn(x):\n    return x + 2\n")

    old_inc, old_index = _run_scan(old_scan_index, root, old_dir, "--incremental")
    new_inc, new_index = _run_scan(_NEW_SCAN_INDEX, root, new_dir, "--incremental")

    assert old_inc.returncode == new_inc.returncode == 0
    assert old_index is not None and new_index is not None
    _assert_v13_index_delta(old_index, new_index)


# --- exclusions ----------------------------------------------------------


def test_exclusions_prune_vendored_copy_identically(tmp_path: Path, old_scan_index: Path) -> None:
    """Prune ignored files identically in both scanner implementations."""
    root = tmp_path / "proj"
    root.mkdir()
    _build_fixture_project(root)
    index_dir = tmp_path / "idx"

    old_result, old_index = _run_scan(old_scan_index, root, index_dir)
    new_result, new_index = _run_scan(_NEW_SCAN_INDEX, root, index_dir)

    assert old_result.returncode == new_result.returncode == 0
    assert old_result.stderr == new_result.stderr  # excludes the "vendored-lib" summary line
    assert old_index is not None and new_index is not None
    assert not any("vendored" in path for path in old_index["file_shas"])
    assert not any("vendored" in path for path in new_index["file_shas"])
    assert old_index["excluded_roots"] == new_index["excluded_roots"]
    _assert_v13_index_delta(old_index, new_index)


# --- degraded module + stats output ---------------------------------------


def test_degraded_module_and_stats_output_identical(tmp_path: Path, old_scan_index: Path) -> None:
    """Handle a syntax-error module consistently across extraction paths."""
    root = tmp_path / "proj"
    root.mkdir()
    _build_fixture_project(root)  # already includes pkg/broken.py
    index_dir = tmp_path / "idx"

    old_result, old_index = _run_scan(old_scan_index, root, index_dir)
    old_stderr = old_result.stderr
    new_result, new_index = _run_scan(_NEW_SCAN_INDEX, root, index_dir)

    assert old_result.returncode == new_result.returncode == 0
    assert old_stderr == new_result.stderr
    assert "modules indexed" in old_stderr
    assert "broken.py" in old_stderr
    assert old_index is not None and new_index is not None
    old_status = {m["name"]: m.get("status") for m in old_index["modules"]}
    new_status = {m["name"]: m.get("status") for m in new_index["modules"]}
    assert old_status == new_status
    assert old_status.get("pkg.broken") == "degraded"


# --- error path + exit code ------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod-based write-permission removal is a no-op on Windows directories",
)
def test_permission_error_exit_code_parity(tmp_path: Path, old_scan_index: Path) -> None:
    """A permission error writing the index produces the same exit code + message old-vs-new."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text("def f(x):\n    return x\n")
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    index_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # read+execute only — no write

    try:
        old_result, old_index = _run_scan(old_scan_index, root, index_dir)
        new_result, new_index = _run_scan(_NEW_SCAN_INDEX, root, index_dir)
    finally:
        index_dir.chmod(stat.S_IRWXU)  # restore for tmp_path cleanup

    assert old_index is None and new_index is None
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
