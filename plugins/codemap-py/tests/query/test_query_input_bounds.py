"""Reject unsafe query inputs before reading source or running expensive searches."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path

import pytest
from _host_skips import skip_file_symlink_unavailable
from codemap_py import query_state as state
from codemap_py.query import coverage, diff_batch, index_io, symbols


@pytest.mark.parametrize(
    "reader",
    [
        pytest.param(diff_batch._git_diff_paths, id="file-list"),
        pytest.param(diff_batch._git_diff_line_ranges, id="line-ranges"),
    ],
)
def test_diff_base_cannot_create_output_file(tmp_path, reader):
    """An option-shaped base never reaches Git's output-file option."""
    target = tmp_path / "output"
    args = [f"--output={target}"]
    extra = ["example.py"] if reader is diff_batch._git_diff_line_ranges else []
    result = reader(*args, *extra)
    assert not target.exists()
    assert result == [] or result.get("error") == "invalid base"


@pytest.mark.parametrize("pattern", [".*.*.*.*.*.*!", ".+.+.+.+!", "a{1,}b{1,}c{1,}d{1,}!"])
def test_separated_unbounded_quantifiers_are_rejected(pattern):
    """Separated repeats cannot bypass the regex complexity guard."""
    assert symbols._is_dangerous_regex(pattern)


@pytest.mark.parametrize("pattern", [r"\*\*\*\*", "[+*][+*][+*][+*]", "^Auth.*Handler$", "a{1,4}b{1,4}"])
def test_literal_or_bounded_quantifiers_remain_searchable(pattern):
    """Escaped punctuation, classes and bounded repeats are safe query syntax."""
    assert not symbols._is_dangerous_regex(pattern)


def _symbol_entry() -> dict:
    """Return a source coordinate independent of the indexed file location."""
    return {"name": "secret", "qualified_name": "secret", "type": "function", "start_line": 1, "end_line": 2}


@pytest.mark.parametrize("path_kind", ["parent", "absolute"])
def test_symbol_refuses_outside_source(tmp_path, path_kind):
    """Index paths cannot turn a symbol lookup into an arbitrary source read."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("def secret():\n    return 'private'\n")
    rel = "../secret.py" if path_kind == "parent" else str(outside)
    with pytest.raises(SystemExit):
        symbols._symbol_group_results(rel, [({"name": "secret"}, _symbol_entry())], root, True)


@skip_file_symlink_unavailable
def test_symbol_refuses_symlink_outside_source(tmp_path):
    """A lexically relative module must also resolve inside the project."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("def secret():\n    return 'private'\n")
    (root / "secret.py").symlink_to(outside)
    with pytest.raises(SystemExit):
        symbols._symbol_group_results("secret.py", [({"name": "secret"}, _symbol_entry())], root, True)


@skip_file_symlink_unavailable
def test_symbol_rejects_cyclic_source_path_as_json(tmp_path, capsys):
    """An unresolvable indexed source produces a bounded error instead of a traceback."""
    (tmp_path / "secret.py").symlink_to("secret.py")
    with pytest.raises(SystemExit) as exc:
        symbols._symbol_group_results("secret.py", [({"name": "secret"}, _symbol_entry())], tmp_path, True)
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid symbol path"


def test_untrusted_scan_root_cannot_redirect_source(tmp_path, monkeypatch):
    """A stored root must agree with the invoking project or the index's cache location."""
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(index_io, "_fingerprint_root", root)
    monkeypatch.setattr(state, "_LOADED_INDEX_PATH", str(root / ".cache/codemap/project.json"))
    assert index_io._resolve_project_root(None, {"scan_root": str(tmp_path)}) == root.resolve()


@pytest.mark.parametrize(
    "module",
    [
        pytest.param({}, id="missing-name"),
        pytest.param({"name": []}, id="non-string-name"),
        pytest.param({"name": "m", "symbols": [{}]}, id="missing-symbol-fields"),
        pytest.param({"name": "m", "symbols": [None]}, id="non-object-symbol"),
        pytest.param({"name": "m", "path": "../secret.py"}, id="parent-path"),
        pytest.param({"name": "m", "path": "C:\\private\\secret.py"}, id="windows-absolute-path"),
        pytest.param({"name": "m", "path": "/private/secret.py"}, id="posix-absolute-path"),
        pytest.param({"name": "m", "import_groups": []}, id="non-object-import-groups"),
        pytest.param({"name": "m", "import_groups": {"stdlib": [[]]}}, id="non-string-group-name"),
        pytest.param({"name": "m", "fixture_exports": [None]}, id="non-object-fixture-export"),
        pytest.param({"name": "m", "fixture_exports": [{}]}, id="unnamed-fixture-export"),
        pytest.param({"name": "m", "fixture_exports": [{"name": "f", "params": [[]]}]}, id="bad-fixture-param"),
        pytest.param({"name": "m", "fixture_uses": [{}]}, id="unnamed-fixture-use"),
        pytest.param({"name": "m", "mock_patches": [None]}, id="non-object-mock-patch"),
        pytest.param({"name": "m", "subprocess_calls": [None]}, id="non-object-subprocess-call"),
    ],
)
def test_load_rejects_malformed_entries(tmp_path, capsys, module):
    """Malformed module and symbol records fail as JSON before handler dispatch."""
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"scan_version": 11, "modules": [module]}))
    with pytest.raises(SystemExit) as exc:
        index_io.load_index(path)
    assert exc.value.code == 3
    assert json.loads(capsys.readouterr().out)["error"] == "index failed self-check"


@pytest.mark.parametrize(
    "calls",
    [
        pytest.param([None], id="non-object-call"),
        pytest.param([{"resolution": "internal"}], id="missing-call-target"),
        pytest.param([{"resolution": []}], id="unhashable-resolution"),
    ],
)
def test_load_rejects_malformed_call_entries(tmp_path, calls):
    """Malformed call records fail before call-graph handlers index their fields."""
    path = tmp_path / "index.json"
    symbol = {**_symbol_entry(), "calls": calls}
    path.write_text(json.dumps({"scan_version": 11, "modules": [{"name": "m", "symbols": [symbol]}]}))
    with pytest.raises(SystemExit) as exc:
        index_io.load_index(path)
    assert exc.value.code == 3


def test_nul_stored_root_is_rejected_at_load(tmp_path):
    """Malformed root metadata cannot bypass the bounded load error."""
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"scan_version": 11, "modules": [], "scan_root": "\0"}))
    with pytest.raises(SystemExit) as exc:
        index_io.load_index(path)
    assert exc.value.code == 3


@skip_file_symlink_unavailable
def test_cyclic_stored_root_cannot_redirect_query(tmp_path, monkeypatch):
    """Unrelated cyclic root metadata stays unresolved and cannot redirect source reads."""
    loop = tmp_path / "loop"
    loop.symlink_to("loop", target_is_directory=True)
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"scan_version": 11, "modules": [], "scan_root": str(loop)}))
    monkeypatch.setattr(index_io, "_fingerprint_root", tmp_path)
    monkeypatch.setattr(state, "_LOADED_INDEX_PATH", str(path))
    index = index_io.load_index(path)
    assert index_io._resolve_project_root(None, index) == tmp_path.resolve()
    assert index_io._detect_root_mismatch(tmp_path, index) is True


@pytest.mark.parametrize("offset", [60_000, math.inf, math.nan])
def test_future_or_nonfinite_session_marker_is_rejected(offset):
    """Repository markers cannot reserve a session indefinitely."""
    assert coverage._valid_session_id({"session_id": "chosen", "ts": time.time() * 1000 + offset}) is None


def test_coverage_sentinel_uses_private_directory(tmp_path, monkeypatch):
    """Sentinels are created in a user directory rather than the shared temp root."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    assert coverage._coverage_already_emitted("session") is False
    files = list(tmp_path.rglob("*"))
    sentinel = next(path for path in files if path.is_file())
    assert sentinel.parent != tmp_path
    assert coverage._coverage_already_emitted("session") is True


@skip_file_symlink_unavailable
def test_coverage_sentinel_refuses_symlink(tmp_path, monkeypatch):
    """An existing symlink cannot suppress full coverage or alter its target."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    assert coverage._coverage_already_emitted("session") is False
    sentinel = next(path for path in tmp_path.rglob("*") if path.is_file())
    sentinel.unlink()
    target = tmp_path / "target"
    target.write_text("keep")
    sentinel.symlink_to(target)
    assert coverage._coverage_already_emitted("session") is False
    assert target.read_text() == "keep"


@skip_file_symlink_unavailable
def test_coverage_sentinel_refuses_symlink_directory(tmp_path, monkeypatch):
    """A planted user-directory symlink fails verbose before creating a sentinel."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    identity = str(os.getuid()) if hasattr(os, "getuid") else str(Path.home())
    directory = tmp_path / f"codemap-coverage-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
    target = tmp_path / "external"
    target.mkdir()
    directory.symlink_to(target, target_is_directory=True)
    assert coverage._coverage_already_emitted("session") is False
    assert list(target.iterdir()) == []
