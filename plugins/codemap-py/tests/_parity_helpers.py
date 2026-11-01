"""Shared helpers for the extraction-parity suites under ``tests/parity``.

Each parity suite compares the frozen pre-extraction monolith (``tests/data/parity_golden``) against the current thin
``bin/`` launcher. The golden checkout, the v12/v13 index normalisation, the subprocess scan runner and the feature-rich
fixture project are identical across those suites, so they live here once. Importable from every test module because
``conftest.py`` puts this directory on ``sys.path``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

TESTS_DIR = Path(__file__).resolve().parent
NEW_SCAN_INDEX = TESTS_DIR.parent / "bin" / "scan-index"
PARITY_GOLDEN_DIR = TESTS_DIR / "data" / "parity_golden"

PATH_CLASSES = ["proj", "proj café ünïcode dir"]

# Frozen as static fixtures under tests/data/parity_golden/ rather than read via
# `git show HEAD:...` — a HEAD-pinned checkout silently degrades to the
# post-extraction thin launcher once the extraction commit lands (HEAD then
# advances past the monolith), and is simply absent in a shallow CI clone that
# never fetched the old SHA. See assert_is_monolith for the loud-failure guard
# against a golden fixture being accidentally overwritten in kind.
_OLD_BIN_SIBLINGS = ("_exclusions.py", "_schema.py", "_telemetry.py")
VOLATILE_KEYS = ("scanned_at", "git_sha")
V12_ROOT_ADDITIONS = frozenset({"symbol_aliases", "symbol_alias_limitations"})
V12_MODULE_ADDITIONS = frozenset({"symbol_aliases", "symbol_alias_limitations"})
V13_MODULE_ADDITIONS = frozenset({"unresolved_direct_imports", "from_import_submodules"})

# The monolith is roughly 10x-100x larger than its post-extraction thin
# launcher/shim counterpart; thresholds sit with headroom below the monolith
# line count and well above the current thin-file line count.
_MONOLITH_MIN_LINES = {
    "scan-index": 500,
    "scan-query": 500,
    "_exclusions.py": 100,
    "_schema.py": 50,
    "_telemetry.py": 50,
}

# Substrings present in every current (post-extraction) bin/ shim or launcher
# docstring, absent from every pre-extraction monolith source — their presence
# in a golden fixture means it was accidentally overwritten with
# post-extraction content instead of holding the frozen monolith.
_POST_EXTRACTION_MARKERS = ("codemap_py", "thin launcher", "compatibility shim")


def assert_is_monolith(name: str, content: str) -> None:
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


def materialize_old_bin(dest: Path, script: str) -> Path:
    """Copy the frozen pre-extraction monolithic *script* (``scan-index`` or ``scan-query``) + siblings into *dest*.

    Returns the path to the copied *script*, written under its literal basename: argparse derives ``prog`` from
    ``os.path.basename(sys.argv[0])``, so keeping the golden script's basename identical to the current ``bin/``
    launcher's keeps even the usage/error banners byte-identical between old and new. Source is the static
    ``tests/data/parity_golden/`` fixture (captured once from ``HEAD`` at the time of the extraction task) — not a live
    ``git show HEAD:...`` checkout, which would silently start returning the post-extraction thin launcher the moment
    the extraction commit lands.
    """
    for name in (script, *_OLD_BIN_SIBLINGS):
        content = (PARITY_GOLDEN_DIR / name).read_text()
        assert_is_monolith(name, content)
        (dest / name).write_text(content)
    return dest / script


def legacy_index(index: dict) -> dict:
    """Normalize v12/v13 additions while restoring legacy import metrics.

    >>> legacy_index({"modules": []})
    {'modules': []}
    """
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
            key: value for key, value in normalized.items() if key not in V12_MODULE_ADDITIONS | V13_MODULE_ADDITIONS
        }
        modules.append(normalized)
    legacy = {
        key: value
        for key, value in index.items()
        if key not in VOLATILE_KEYS
        and key not in V12_ROOT_ADDITIONS
        and key not in {"scan_version", "source_fingerprint"}
    }
    legacy["modules"] = modules
    if "file_shas" in legacy:
        legacy["file_shas"] = {key: value for key, value in legacy["file_shas"].items() if key != ".codemapignore"}
    return legacy


def canonical(index: dict) -> str:
    """Serialize an index with stable key ordering for parity assertions.

    >>> canonical({"b": 2, "a": 1})
    '{"a": 1, "b": 2}'
    """
    return json.dumps(index, sort_keys=True, ensure_ascii=False)


def assert_v13_index_delta(old: dict, new: dict) -> None:
    """Prove frozen v11 content is identical except declared v12/v13 graph semantics."""
    assert old["scan_version"] == 11
    assert new["scan_version"] == 13
    assert set(new) == set(old) | V12_ROOT_ADDITIONS | {"source_fingerprint"}
    # These non-Git fixtures retain every historical digest, plus the newly consumed ignore-policy input.
    expected_hashes = dict(old["file_shas"])
    ignore_file = Path(new["scan_root"]) / ".codemapignore"
    if ignore_file.exists():
        expected_hashes[".codemapignore"] = hashlib.md5(ignore_file.read_bytes(), usedforsecurity=False).hexdigest()
    assert new["file_shas"] == expected_hashes
    sources = {key: value for key, value in expected_hashes.items() if key != "__coverage_mtime__"}
    payload = json.dumps([13, sources], sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert new["source_fingerprint"] == hashlib.sha256(payload).hexdigest()
    assert len(old["modules"]) == len(new["modules"])
    for legacy_module, current_module in zip(old["modules"], new["modules"], strict=True):
        additions = V12_MODULE_ADDITIONS | V13_MODULE_ADDITIONS if current_module["status"] == "ok" else frozenset()
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
    assert canonical(legacy_index(old)) == canonical(legacy_index(new))


def corrupt_project(new: dict) -> None:
    """Change legacy-visible content that the declared v13 metadata delta must not hide."""
    new["project"] = "corrupt"


def corrupt_input_hash(new: dict) -> None:
    """Change one recorded input-file digest."""
    new["file_shas"]["pkg/leaf.py"] = "0" * 32


def corrupt_fingerprint(new: dict) -> None:
    """Change the recorded source fingerprint."""
    new["source_fingerprint"] = "corrupt"


def run_scan(
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


def build_fixture_project(root: Path) -> None:
    """Write a small, feature-rich project used across the parity scenarios.

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
    # is_test stays False, silently skipping fixture-graph and mock-patch extraction.
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


class ScanPair(NamedTuple):
    """Old-engine and new-engine results of scanning one fixture root into one index directory."""

    old_result: subprocess.CompletedProcess
    old_index: dict
    new_result: subprocess.CompletedProcess
    new_index: dict


def scan_old_and_new(base: Path, root_name: str, old_scan_index: Path) -> ScanPair:
    """Scan the feature-rich fixture once with the golden monolith and once with the current launcher.

    Both engines scan the identical root path (``base / root_name``) into the identical index directory sequentially, so
    path-dependent output (``scan_root``, stdout/stderr embedding ``out_path``) is truly comparable. The result is
    immutable evidence shared by every read-only comparison; tests that corrupt an index must deep-copy it first.
    """
    root = base / root_name
    root.mkdir()
    build_fixture_project(root)
    index_dir = base / "idx"
    old_result, old_index = run_scan(old_scan_index, root, index_dir)
    new_result, new_index = run_scan(NEW_SCAN_INDEX, root, index_dir)
    assert old_index is not None
    assert new_index is not None
    return ScanPair(old_result, old_index, new_result, new_index)
