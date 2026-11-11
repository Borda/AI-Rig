"""Exercise safe index recovery, source limits and fixture-root independence."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from _host_skips import skip_executable_mode_unavailable
from codemap_py import cli
from codemap_py import query_state as state
from codemap_py.query import coverage, diff_batch, index_io, subprocess_fixtures, symbols
from codemap_py.scanner import discovery


@pytest.mark.parametrize("stored", [r"\\server\share\project", r"\\?\C:\project", r"\\.\pipe\project"])
def test_nonlocal_root_rejected_before_resolution(stored, monkeypatch):
    """Reject Windows remote and device roots without filesystem access on every host."""
    monkeypatch.setattr(Path, "resolve", lambda self: pytest.fail("unexpected filesystem resolution"))
    assert not index_io._valid_scan_root(stored)


def test_unrelated_local_root_load_does_not_resolve_it(tmp_path, monkeypatch):
    """Loading metadata cannot resolve an unrelated local root before checking its anchor."""
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"scan_version": 11, "scan_root": str(tmp_path / "unrelated"), "modules": []}))
    monkeypatch.setattr(Path, "resolve", lambda self: pytest.fail("unexpected filesystem resolution"))
    assert index_io.load_index(path)["modules"] == []


@pytest.mark.parametrize("operation", ["resolve", "mismatch"])
def test_unrelated_stored_root_is_not_resolved(tmp_path, monkeypatch, operation):
    """Compare an unrelated stored root lexically before touching its filesystem location."""
    queried = tmp_path / "queried"
    queried.mkdir()
    stored = tmp_path / "unrelated"
    original = Path.resolve

    def guarded_resolve(path, *args, **kwargs):
        """Forbid resolution of the untrusted root while allowing the invoking project."""
        assert path != stored
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    monkeypatch.setattr(index_io, "_fingerprint_root", queried)
    monkeypatch.setattr(state, "_LOADED_INDEX_PATH", str(queried / ".cache/codemap/queried.json"))
    action = index_io._resolve_project_root if operation == "resolve" else index_io._detect_root_mismatch
    expected = queried.resolve() if operation == "resolve" else True
    assert action(None if operation == "resolve" else queried, {"scan_root": str(stored)}) == expected


@pytest.mark.skipif(os.name == "nt", reason="colon is not a legal native Windows filename")
def test_colon_posix_module_loads(tmp_path):
    """A colon-bearing native POSIX filename does not invalidate the entire index."""
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"scan_version": 11, "modules": [{"name": "m", "path": "a:b.py"}]}))
    assert index_io.load_index(path)["modules"][0]["path"] == "a:b.py"


@pytest.mark.parametrize("version", [3, 11])
def test_legacy_optional_symbol_fields_load(tmp_path, version):
    """Load supported old records whose symbol type and call resolution were omitted."""
    symbol = {"name": "f", "qualified_name": "f", "start_line": 1, "end_line": 1, "calls": [{"target": "m::g"}]}
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"scan_version": version, "modules": [{"name": "m", "symbols": [symbol]}]}))
    assert index_io.load_index(path)["modules"][0]["symbols"] == [symbol]


@pytest.mark.parametrize("field", ["type", "resolution"])
def test_present_nonstring_legacy_field_is_rejected(field):
    """Optional legacy fields remain strictly typed when present."""
    symbol = {"name": "f", "qualified_name": "f", "start_line": 1, "end_line": 1}
    symbol.update({"type": None} if field == "type" else {"calls": [{"target": "m::g", "resolution": None}]})
    assert not index_io._valid_symbol_entry(symbol)


@skip_executable_mode_unavailable
def test_owned_loose_sentinel_directory_is_repaired(tmp_path, monkeypatch):
    """Repair the user's directory so subsequent queries can compact coverage."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    identity = os.getuid() if hasattr(os, "getuid") else 42
    monkeypatch.setattr(coverage.os, "getuid", lambda: identity, raising=False)
    assert coverage._coverage_already_emitted("first") is False
    directory = next(tmp_path.iterdir())
    original = Path.lstat

    def loose_stat(path, *args, **kwargs):
        """Expose a loose owned mode independently of host UID support."""
        info = original(path, *args, **kwargs)
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=identity) if path == directory else info

    monkeypatch.setattr(Path, "lstat", loose_stat)
    directory.chmod(0o755)
    assert coverage._coverage_already_emitted("next") is False
    assert directory.stat().st_mode & 0o077 == 0
    assert coverage._coverage_already_emitted("next") is True


def test_foreign_sentinel_owner_is_diagnosed(tmp_path, monkeypatch, capsys):
    """Report a foreign-owned coverage directory without writing a sentinel."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(coverage.os, "getuid", lambda: 42, raising=False)
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=43))
    assert coverage._coverage_already_emitted("session") is False
    assert "owner" in capsys.readouterr().err


@pytest.mark.parametrize(
    "reader",
    [pytest.param(diff_batch._git_diff_paths, id="paths"), pytest.param(diff_batch._git_diff_line_ranges, id="lines")],
)
def test_diff_uses_portable_git_options(monkeypatch, reader):
    """Diff remains usable with Git versions lacking the newer option terminator."""

    def old_git(command, **kwargs):
        """Model Git that accepts the established diff syntax only."""
        assert "--end-of-options" not in command
        return "" if kwargs.get("text") else b""

    monkeypatch.setattr(diff_batch.subprocess, "check_output", old_git)
    args = ["HEAD", "example.py"] if reader is diff_batch._git_diff_line_ranges else ["HEAD"]
    assert reader(*args) == []


def test_each_fixture_root_expands_shared_dependencies(capsys):
    """Every requested root gets a full tree while within-root diamonds use references."""
    index = {
        "scan_version": 11,
        "modules": [
            {
                "name": "conftest",
                "path": "conftest.py",
                "status": "ok",
                "fixture_exports": [
                    {"name": "a", "params": ["shared"]},
                    {"name": "b", "params": ["shared"]},
                    {"name": "shared", "params": ["leaf"]},
                ],
            },
            {
                "name": "tests.test_example",
                "status": "ok",
                "is_test": True,
                "fixture_uses": [{"name": "a"}, {"name": "b"}],
            },
        ],
    }
    subprocess_fixtures.cmd_fixture_graph(index, "tests.test_example")
    roots = json.loads(capsys.readouterr().out)["fixtures"]
    assert roots[0]["depends_on"][0]["depends_on"][0]["name"] == "leaf"
    assert roots[1]["depends_on"][0]["depends_on"][0]["name"] == "leaf"


@pytest.mark.parametrize("kind", ["directory", "oversized"])
def test_symbol_source_requires_bounded_regular_file(tmp_path, monkeypatch, capsys, kind):
    """Reject directory sources and over-limit files with a bounded JSON diagnostic."""
    source = tmp_path / "example.py"
    source.mkdir() if kind == "directory" else source.write_bytes(b"x" * 65)
    monkeypatch.setattr(discovery, "_MAX_FILE_SIZE_BYTES", 64)
    symbol = {"name": "f", "qualified_name": "f", "type": "function", "start_line": 1, "end_line": 1}
    with pytest.raises(SystemExit) as exc:
        symbols._symbol_group_results("example.py", [({"name": "m"}, symbol)], tmp_path, False)
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid symbol source"


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(KeyError("field"), id="key"),
        pytest.param(TypeError("bad field"), id="type"),
        pytest.param(ValueError("bad field"), id="value"),
    ],
)
def test_query_dispatch_reports_malformed_handler_record(monkeypatch, capsys, tmp_path, error):
    """The unified dispatcher converts schema gaps into the established self-check error."""

    def malformed_record(argv):
        """Model a query handler encountering a malformed unvalidated field."""
        raise error

    monkeypatch.setattr(cli.query, "main", malformed_record)
    assert cli._run_query([], tmp_path) == 3
    output = capsys.readouterr()
    assert json.loads(output.out)["error"] == "index failed self-check"
    assert type(error).__name__ in output.err


def test_query_reports_real_malformed_centrality(tmp_path, monkeypatch, capsys):
    """A valid-looking index with a malformed count produces a self-check error at query time."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCAN_NO_AUTOBUILD", "1")
    # The real dispatcher records process-wide query state; register both so teardown restores them.
    monkeypatch.setattr(index_io, "_fingerprint_root", index_io._fingerprint_root)
    monkeypatch.setattr(state, "_LOADED_INDEX_PATH", state._LOADED_INDEX_PATH)
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"scan_version": 11, "modules": [{"name": "m", "rdep_count": "bad"}]}))
    result = cli._run_query(["--index", str(path), "--root", str(tmp_path), "central"], tmp_path)
    output = capsys.readouterr()
    assert result == 3
    assert json.loads(output.out)["error"] == "index failed self-check"
    assert "TypeError" in output.err


def test_legacy_symbol_without_type_can_be_listed(tmp_path, capsys):
    """Accepted legacy symbols remain usable by the symbol-list handler."""
    path = tmp_path / "index.json"
    path.write_text(
        json.dumps(
            {
                "scan_version": 3,
                "modules": [
                    {"name": "m", "symbols": [{"name": "f", "qualified_name": "f", "start_line": 1, "end_line": 1}]}
                ],
            }
        )
    )
    symbols.cmd_symbols(index_io.load_index(path), "m")
    assert json.loads(capsys.readouterr().out)["symbols"][0]["type"] == ""
