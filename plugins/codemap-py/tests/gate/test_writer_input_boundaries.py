"""Exercise bounded writer reads and symlink-resistant publication."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest
from _host_skips import skip_file_symlink_unavailable
from codemap_py import graph, query, rwgate


@pytest.mark.parametrize(
    ("version", "rebuilt", "diagnostic"),
    [
        pytest.param(13, True, "falling back to full scan", id="previous-generation-rebuilt"),
        pytest.param(14, False, "Incremental scan", id="current-generation-reused"),
    ],
)
def test_incremental_schema_generation_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: int, rebuilt: bool, diagnostic: str
) -> None:
    """Rebuild version thirteen even with matching sources, and load version fourteen unchanged."""
    (tmp_path / "module.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    index_path = tmp_path / "index.json"
    previous = graph.scan(tmp_path)
    previous["scan_version"] = version
    previous["scanned_at"] = "2000-01-01T00:00:00Z"
    index_path.write_text(json.dumps(previous), encoding="utf-8")
    capsys.readouterr()
    args = argparse.Namespace(incremental=True, with_coverage=None)

    result = graph._build_index(args, tmp_path, index_path)

    assert result["scan_version"] == 14
    assert (result["scanned_at"] != previous["scanned_at"]) is rebuilt
    assert diagnostic in capsys.readouterr().err
    index_path.write_text(json.dumps(result), encoding="utf-8")
    assert query.load_index(index_path) == result


@pytest.mark.parametrize("contents", ["{broken", "[]", '{"scan_version":null}', '{"scan_version":"bad"}'])
def test_corrupt_incremental_index_is_bounded(tmp_path: Path, contents: str) -> None:
    """Malformed previous generations raise the gate's bounded exception."""
    index = tmp_path / "index.json"
    index.write_text(contents, encoding="utf-8")
    args = argparse.Namespace(incremental=True, with_coverage=None)

    with pytest.raises(rwgate.IndexUnreadable):
        graph._build_index(args, tmp_path, index)


@pytest.mark.parametrize("contents", ["{broken", '{"scan_version":null}'])
def test_incremental_cli_reports_structured_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], contents: str
) -> None:
    """The actual indexing entrypoint returns exit one and bounded JSON for a corrupt old index."""
    index = tmp_path / ".cache/codemap" / f"{tmp_path.name}.json"
    index.parent.mkdir(parents=True)
    index.write_text(contents, encoding="utf-8")
    monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
    monkeypatch.delenv("CODEMAP_COORDINATION_DIR", raising=False)
    monkeypatch.setattr(sys, "argv", ["scan-index", "--root", str(tmp_path), "--incremental"])

    with pytest.raises(SystemExit) as error:
        graph.main()

    captured = capsys.readouterr()
    assert error.value.code == 1
    assert json.loads(captured.err.splitlines()[-1])["error"] == "index_unreadable"
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("incremental", [False, True])
def test_writer_caps_existing_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, incremental: bool) -> None:
    """Writer reads refuse oversized data before passing it to the JSON parser."""
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"scan_version": graph.SCAN_VERSION, "file_shas": {}, "padding": "x" * 100}))
    monkeypatch.setattr(rwgate, "_MAX_INDEX_SIZE_BYTES", 64, raising=False)
    args = argparse.Namespace(incremental=incremental, with_coverage=None)

    with pytest.raises(rwgate.IndexUnreadable, match="too large"):
        graph._build_index(args, tmp_path, index) if incremental else rwgate._load_index(index)


@skip_file_symlink_unavailable
@pytest.mark.parametrize("component", [".cache", ".cache/codemap"])
def test_default_index_directory_cannot_escape_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    """Repository cache symlinks cannot redirect publication outside the project."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)

    with pytest.raises(rwgate.CoordinationUnavailable):
        graph._resolve_out_path(root)

    assert list(outside.iterdir()) == []


@skip_file_symlink_unavailable
@pytest.mark.parametrize("name", ["registry.lock", "writer.json", "readers"])
def test_symlinked_coordination_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Coordination symlinks neither overwrite victims nor escape as raw OS errors."""
    monkeypatch.delenv("CODEMAP_COORDINATION_DIR", raising=False)
    coord = tmp_path / ".index-rw"
    coord.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"original")
    (coord / name).symlink_to(victim)

    with pytest.raises(rwgate.CoordinationUnavailable):
        rwgate.write_index(tmp_path / "index.json", lambda path: None, timeout=0.1)

    assert victim.read_bytes() == b"original"


@skip_file_symlink_unavailable
def test_dangling_registry_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dangling registry lock reports a bounded coordination error."""
    monkeypatch.delenv("CODEMAP_COORDINATION_DIR", raising=False)
    coord = tmp_path / ".index-rw"
    coord.mkdir()
    (coord / "registry.lock").symlink_to(tmp_path / "missing")

    with pytest.raises(rwgate.CoordinationUnavailable):
        rwgate.write_index(tmp_path / "index.json", lambda path: None, timeout=0.1)


@skip_file_symlink_unavailable
def test_event_log_does_not_follow_symlink(tmp_path: Path) -> None:
    """Optional gate telemetry cannot append to a symlink victim."""
    victim = tmp_path / "victim"
    victim.write_bytes(b"original")
    log = tmp_path / "events.jsonl"
    log.symlink_to(victim)

    rwgate._append_event_line(str(log), "event", {})

    assert victim.read_bytes() == b"original"


@skip_file_symlink_unavailable
def test_owned_token_does_not_follow_symlink(tmp_path: Path) -> None:
    """A token creator never truncates an existing symlink target."""
    victim = tmp_path / "victim"
    victim.write_bytes(b"original")
    token = tmp_path / "writer.json"
    token.symlink_to(victim)
    registry = rwgate._Registry(tmp_path, 1)

    with pytest.raises(rwgate.CoordinationUnavailable):
        rwgate._create_owned(registry, token, b"payload")

    assert victim.read_bytes() == b"original"


@skip_file_symlink_unavailable
@pytest.mark.parametrize("stream", [False, True])
def test_publication_temp_does_not_follow_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    """Even a precreated temporary name cannot redirect publication bytes."""
    victim = tmp_path / "victim"
    victim.write_bytes(b"original")
    temp = tmp_path / ".index.tmp"
    temp.symlink_to(victim)
    monkeypatch.setattr(rwgate, "_temp_beside", lambda target: temp)

    with pytest.raises(FileExistsError):
        _publish(tmp_path / "index.json", stream)

    assert victim.read_bytes() == b"original"


def _publish(index: Path, stream: bool) -> None:
    """Publish the same bytes through either public writer helper."""
    if stream:
        with rwgate.publish_stream(index) as handle:
            handle.write("{}")
    else:
        rwgate.atomic_publish(index, b"{}")
