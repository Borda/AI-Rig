"""Persist project-relative index paths and path-free CLI provenance."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from codemap_py import graph, index_paths, telemetry
from codemap_py import query_state as state
from codemap_py.query import index_io


def test_full_and_incremental_indexes_use_project_coordinates(tmp_path: Path) -> None:
    """A real scan and unchanged incremental refresh never retain host paths in stored coordinates."""
    (tmp_path / "worker.py").write_text("def run():\n    return 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_runner.py").write_text(
        "import subprocess\nfrom unittest.mock import patch\n"
        "@patch('worker.run')\ndef test_runner():\n    subprocess.run(['python', '../worker.py'])\n"
    )
    index = graph.scan(tmp_path)
    refreshed = graph.incremental_scan(tmp_path, index)
    assert index["scan_root"] == refreshed["scan_root"] == "."
    assert index["scan_root_key"] == refreshed["scan_root_key"] == index_paths.root_key(tmp_path.resolve())
    module = next(module for module in index["modules"] if module["path"] == "tests/test_runner.py")
    refreshed_module = next(module for module in refreshed["modules"] if module["path"] == "tests/test_runner.py")
    assert module["subprocess_calls"] == [{"target_module": "worker", "file": "tests/test_runner.py", "line": 5}]
    assert module["mock_patches"] == [
        {"target": "worker::run", "file": "tests/test_runner.py", "line": 3, "form": "decorator"}
    ]
    assert refreshed_module["subprocess_calls"] == module["subprocess_calls"]
    assert refreshed_module["mock_patches"] == module["mock_patches"]


def test_relative_root_resolves_from_loaded_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreign CWD cannot change source resolution for an index anchored in its project cache."""
    target = tmp_path / "target"
    target.mkdir()
    index = {"scan_root": ".", "scan_root_key": index_paths.root_key(target.resolve())}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(index_io, "_fingerprint_root", tmp_path)
    monkeypatch.setattr(state, "_LOADED_INDEX_PATH", str(target / ".cache/codemap/target.json"))
    assert index_io._resolve_project_root(None, index) == target.resolve()
    assert not index_io._detect_root_mismatch(target, index)
    assert index_io._detect_root_mismatch(tmp_path, index)


def test_relative_root_keeps_override_collision_detection(tmp_path: Path) -> None:
    """A digest retains the identity check for equal-basename projects sharing an index override."""
    first = tmp_path / "first" / "project"
    second = tmp_path / "second" / "project"
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "project.json").write_text(
        json.dumps({"scan_root": ".", "scan_root_key": index_paths.root_key(first.resolve())})
    )
    own = index_paths.resolve_index(root=first, index_dir_override=shared)
    foreign = index_paths.resolve_index(root=second, index_dir_override=shared)
    assert own.diagnostics == ()
    assert [diagnostic.code for diagnostic in foreign.diagnostics] == [index_paths.INDEX_ROOT_COLLISION]


@pytest.mark.parametrize("external", ["/home/private/person/input.json", r"C:\Users\private\input.json"])
def test_telemetry_paths_are_relative_or_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external: str
) -> None:
    """CLI logs retain flags and local coordinates while excluding native and foreign absolute paths."""
    monkeypatch.setenv("CODEMAP_LOGGING", "true")
    monkeypatch.setenv("CODEMAP_RUNTIME", "direct")
    monkeypatch.setenv("CODEMAP_TELEMETRY_SESSION", "coordinate-test")
    monkeypatch.setenv("CODEMAP_LOG_DIR", str(tmp_path / "logs"))
    telemetry.log_cli(
        "symbol",
        ["--root", str(tmp_path), "--index=" + str(tmp_path / "index.json"), "pkg.mod::run", external],
        {},
        time.time(),
        root=tmp_path,
    )
    record = json.loads((tmp_path / "logs/direct/cli_coordinate-test.jsonl").read_text())
    assert record["project"] == index_paths.root_key(tmp_path.resolve())
    assert record["argv"] == ["--root", ".", "--index=index.json", "pkg.mod::run", "<external-path>"]
    assert external not in json.dumps(record)


def test_incremental_refresh_migrates_unchanged_legacy_coordinates(tmp_path: Path) -> None:
    """An unchanged absolute-root index rebuilds its carried mock and subprocess file coordinates."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "worker.py").write_text("def run():\n    return 1\n")
    source = tmp_path / "tests/test_runner.py"
    source.write_text(
        "import subprocess\nfrom unittest.mock import patch\n"
        "@patch('worker.run')\ndef test_runner():\n    subprocess.run(['python', '../worker.py'])\n"
    )
    old = graph.scan(tmp_path)
    old["scan_root"] = str(tmp_path.resolve())
    old.pop("scan_root_key")
    module = next(module for module in old["modules"] if module["path"] == "tests/test_runner.py")
    module["mock_patches"][0]["file"] = str(source)
    module["subprocess_calls"][0]["file"] = str(source)

    refreshed = graph.incremental_scan(tmp_path, old)

    migrated = next(module for module in refreshed["modules"] if module["path"] == "tests/test_runner.py")
    assert refreshed["scan_root"] == "."
    assert refreshed["scan_root_key"] == index_paths.root_key(tmp_path.resolve())
    assert migrated["mock_patches"][0]["file"] == "tests/test_runner.py"
    assert migrated["subprocess_calls"][0]["file"] == "tests/test_runner.py"


@pytest.mark.parametrize("key", [None, "", "0" * 63, "X" * 64])
def test_relative_root_without_valid_identity_is_rejected(key: str | None) -> None:
    """A relative marker without a usable digest cannot bypass root provenance checks."""
    assert index_io._index_entries_reason({"scan_root": ".", "scan_root_key": key, "modules": []}) == "invalid_entries"
