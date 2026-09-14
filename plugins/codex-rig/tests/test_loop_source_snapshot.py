"""Real-Git acceptance checks for loop source snapshots."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
COLLECT_DIFF = PLUGIN_ROOT / "shared" / "collect_diff.py"


def _git(repository: Path, *arguments: str) -> None:
    """Run one successful local Git command in a disposable repository."""
    completed = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def _load_collector() -> ModuleType:
    """Load the standalone collector without installing the plugin."""
    specification = importlib.util.spec_from_file_location("loop_source_snapshot", COLLECT_DIFF)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _supports_symlinks() -> bool:
    """Probe whether this host can create a local symbolic link during collection."""
    with tempfile.TemporaryDirectory() as directory:
        link = Path(directory) / "link"
        try:
            link.symlink_to("target")
        except OSError:
            return False
        return link.is_symlink()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_capture_source_snapshot_binds_worktree_contents_and_index(tmp_path: Path, newline: str) -> None:
    """Preserve selected source bytes and staged state for either newline convention on every host."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Snapshot fixture")
    _git(repository, "config", "user.email", "snapshot@example.invalid")

    source = repository / "source"
    source.mkdir()
    base_content = f"value = 'base'{newline}"
    new_content = f"new source{newline}"
    (source / "module.py").write_bytes(base_content.encode("utf-8"))
    (source / "staged.py").write_bytes(base_content.encode("utf-8"))
    (repository / "removed.py").write_bytes(f"removed = True{newline}".encode("utf-8"))
    _git(repository, "add", "source", "removed.py")
    _git(repository, "commit", "-qm", "fixture")

    (source / "staged.py").write_bytes(f"value = 'staged'{newline}".encode("utf-8"))
    _git(repository, "add", "source/staged.py")
    (source / "staged.py").write_bytes(base_content.encode("utf-8"))
    (source / "nested").mkdir()
    (source / "nested" / "new.txt").write_bytes(new_content.encode("utf-8"))
    (source / "data.bin").write_bytes(b"\x00\xffsource")
    (repository / "removed.py").unlink()

    module = _load_collector()
    snapshot = module.capture_source_snapshot(repository, ["removed.py", "source"])

    assert snapshot["schema_version"] == 1
    assert snapshot["repository"] == repository.resolve().as_posix()
    assert snapshot["scope_paths"] == ["removed.py", "source"]
    assert (
        snapshot["revision"] == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    )
    assert snapshot["index_sha256"] != hashlib.sha256(b"").hexdigest()
    assert snapshot["files"] == [
        {
            "path": "removed.py",
            "kind": "missing",
            "sha256": None,
            "executable": False,
            "encoding": "utf-8",
            "content": "",
        },
        {
            "path": "source/data.bin",
            "kind": "file",
            "sha256": hashlib.sha256(b"\x00\xffsource").hexdigest(),
            "executable": False,
            "encoding": "base64",
            "content": "AP9zb3VyY2U=",
        },
        {
            "path": "source/module.py",
            "kind": "file",
            "sha256": hashlib.sha256(base_content.encode("utf-8")).hexdigest(),
            "executable": False,
            "encoding": "utf-8",
            "content": base_content,
        },
        {
            "path": "source/nested/new.txt",
            "kind": "file",
            "sha256": hashlib.sha256(new_content.encode("utf-8")).hexdigest(),
            "executable": False,
            "encoding": "utf-8",
            "content": new_content,
        },
        {
            "path": "source/staged.py",
            "kind": "file",
            "sha256": hashlib.sha256(base_content.encode("utf-8")).hexdigest(),
            "executable": False,
            "encoding": "utf-8",
            "content": base_content,
        },
    ]


def test_snapshot_cli_writes_canonical_json_bytes(tmp_path: Path) -> None:
    """Serialize the returned source snapshot with one stable JSON representation."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Snapshot fixture")
    _git(repository, "config", "user.email", "snapshot@example.invalid")
    (repository / "source.py").write_text("value = 'source'\n", encoding="utf-8")
    _git(repository, "add", "source.py")
    _git(repository, "commit", "-qm", "fixture")
    output = tmp_path / "snapshot.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(COLLECT_DIFF),
            "--snapshot",
            "--repository",
            str(repository),
            "--scope-path",
            "source.py",
            "--out",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    snapshot = _load_collector().capture_source_snapshot(repository, ["source.py"])
    assert (
        output.read_text(encoding="utf-8") == json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    )


@pytest.mark.parametrize("scope_path", ["/outside", "../outside", ":(top)source", "C:\\outside"])
def test_capture_source_snapshot_rejects_unsafe_scope_paths(tmp_path: Path, scope_path: str) -> None:
    """Reject paths that could escape the repository or activate Git pathspec magic."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")

    with pytest.raises(ValueError, match="scope"):
        _load_collector().capture_source_snapshot(repository, [scope_path])


@pytest.mark.skipif(not _supports_symlinks(), reason="host cannot create symbolic links")
def test_capture_source_snapshot_hashes_symlink_target_without_following(tmp_path: Path) -> None:
    """Represent symlink target bytes even when the target does not exist."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Snapshot fixture")
    _git(repository, "config", "user.email", "snapshot@example.invalid")
    link = repository / "broken-link"
    link.symlink_to("missing-target")
    _git(repository, "add", "broken-link")
    _git(repository, "commit", "-qm", "fixture")

    snapshot = _load_collector().capture_source_snapshot(repository, ["broken-link"])

    assert snapshot["files"] == [
        {
            "path": "broken-link",
            "kind": "symlink",
            "sha256": hashlib.sha256(b"missing-target").hexdigest(),
            "executable": False,
            "encoding": "utf-8",
            "content": "missing-target",
        }
    ]


@pytest.mark.skipif(not _supports_symlinks(), reason="host cannot create symbolic links")
def test_capture_source_snapshot_rejects_scope_symlink_escaping_repository(tmp_path: Path) -> None:
    """Refuse a selected symlink whose resolution would leave the repository."""
    repository = tmp_path / "repository"
    repository.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Snapshot fixture")
    _git(repository, "config", "user.email", "snapshot@example.invalid")
    (repository / "external-link").symlink_to(external)
    _git(repository, "add", "external-link")
    _git(repository, "commit", "-qm", "fixture")

    with pytest.raises(ValueError, match="escapes repository"):
        _load_collector().capture_source_snapshot(repository, ["external-link"])
