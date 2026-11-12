"""Regression coverage for private scan state and hook sentinel write boundaries."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest
import setup_scan_env
from _host_skips import skip_file_symlink_unavailable

_PLUGIN = Path(setup_scan_env.__file__).resolve().parent.parent


def _hook(name: str) -> ModuleType:
    """Load the real installed-layout hook without invoking its CLI guard."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), _PLUGIN / "hooks" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_hook_sentinel(flag: Path, monkeypatch: pytest.MonkeyPatch, writer: str) -> None:
    """Drive a named real hook writer with its ordinary sentinel path."""
    tmp_path = flag.parent
    hook = _hook("inject-preamble")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CODEMAP_RUNTIME", "claude")
    if writer == "timestamp":
        hook.write_timestamp(flag)
    elif writer == "refresh":
        hook.write_refresh_record(flag, "fingerprint")
    elif writer == "stale":
        flag.rename(tmp_path / "codemap-stale-proj-claude-sid")
        hook.collapse_stale_notice("proj", "sid", "sha", "stale", "")
    elif writer == "session":
        marker = tmp_path / ".cache" / "codemap" / "current-session-claude.json"
        marker.parent.mkdir(parents=True)
        flag.rename(marker)
        hook.write_session_marker(tmp_path, "sid")
    elif writer == "seed":
        hook = _hook("seed-session")
        monkeypatch.setattr(hook, "project_name", lambda: "proj")
        monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps({"session_id": "sid"})))
        flag.rename(tmp_path / "codemap-proj-session")
        hook.main()
    else:
        hook = _hook("log-skill-start")
        hook.resolve_session(flag)


@pytest.fixture(name="victim")
def _victim(tmp_path: Path, target_exists: bool) -> Path:
    """Prepare the selected existing or dangling symlink target."""
    victim = tmp_path / "victim"
    if target_exists:
        victim.write_text("unchanged", encoding="utf-8")
    return victim


@skip_file_symlink_unavailable
@pytest.mark.parametrize("writer", ["timestamp", "refresh", "stale", "session", "seed", "skill"])
@pytest.mark.parametrize("target_exists", [False, True])
def test_hook_writes_preserve_symlink_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str, target_exists: bool, victim: Path
) -> None:
    """Each named sentinel writer rejects a planted symlink, including dangling links."""
    flag = tmp_path / "flag"
    flag.symlink_to(victim)

    _write_hook_sentinel(flag, monkeypatch, writer)

    assert victim.exists() == target_exists
    assert not target_exists or victim.read_text(encoding="utf-8") == "unchanged"


def test_scan_state_directory_is_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolved scan files live in a user-owned child instead of a shared temp directory."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    directory = setup_scan_env._resolve_tmpdir()
    assert directory.parent == tmp_path
    assert directory.is_dir()
    assert not directory.is_symlink()
    assert os.name == "nt" or stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_touch_rejects_foreign_owned_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing scan markers must belong to the current user before they can be reused."""
    marker = tmp_path / "marker"
    marker.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(setup_scan_env.os, "getuid", lambda: marker.stat().st_uid + 1, raising=False)
    with pytest.raises(OSError, match="owned by another user"):
        setup_scan_env._touch_nofollow(marker)
    assert marker.read_text(encoding="utf-8") == "unchanged"


def test_safe_writer_replaces_regular_content_without_nofollow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The portable writer creates missing sentinels and truncates old content after validation."""
    hook = _hook("seed-session")
    monkeypatch.delattr(hook.os, "O_NOFOLLOW", raising=False)
    sentinel = tmp_path / "sentinel"
    hook._hookutil.write_sentinel(sentinel, "long previous content")
    hook._hookutil.write_sentinel(sentinel, "new")
    assert sentinel.read_text(encoding="utf-8") == "new"


@skip_file_symlink_unavailable
@pytest.mark.parametrize("writer", ["hook", "scan"])
def test_sentinel_rejects_dangling_link_before_exclusive_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    """A host whose exclusive create follows dangling links never receives the unsafe path."""
    sentinel = tmp_path / "sentinel"
    victim = tmp_path / "missing"
    sentinel.symlink_to(victim)
    original_open = os.open

    def follow_dangling(path: Path, flags: int, mode: int = 0o777) -> int:
        """Model Windows exclusive creation through a dangling file symlink."""
        return original_open(victim if path == sentinel else path, flags, mode)

    monkeypatch.setattr(os, "open", follow_dangling)
    writers = {
        "hook": lambda: _hook("seed-session")._hookutil.write_sentinel(sentinel, "redirected"),
        "scan": lambda: setup_scan_env._touch_nofollow(sentinel),
    }
    with pytest.raises(OSError, match="not a regular file"):
        writers[writer]()
    assert sentinel.is_symlink()
    assert not victim.exists()


@skip_file_symlink_unavailable
def test_safe_writer_rejects_swap_without_nofollow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A symlink swapped after inspection cannot redirect writes even without O_NOFOLLOW."""
    hook = _hook("seed-session")
    monkeypatch.delattr(hook.os, "O_NOFOLLOW", raising=False)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("original", encoding="utf-8")
    victim = tmp_path / "victim"
    victim.write_text("untouched", encoding="utf-8")
    original_open = os.open

    def swap(path: Path, flags: int, mode: int = 0o777) -> int:
        """Replace the inspected inode immediately before the existing-file open."""
        if path == sentinel and not flags & os.O_CREAT:
            sentinel.unlink()
            sentinel.symlink_to(victim)
        return original_open(path, flags, mode)

    monkeypatch.setattr(hook.os, "open", swap)
    with pytest.raises(OSError, match="changed while opening"):
        hook._hookutil.write_sentinel(sentinel, "redirected")
    assert victim.read_text(encoding="utf-8") == "untouched"


@skip_file_symlink_unavailable
def test_scan_directory_rejects_planted_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-existing private-directory name cannot redirect setup state to another directory."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    private = setup_scan_env._resolve_tmpdir()
    private.rmdir()
    target = tmp_path / "target"
    target.mkdir()
    private.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError, match="not a real directory"):
        setup_scan_env._resolve_tmpdir()
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("stored_key", ["matching", "foreign", ""])
def test_path_free_index_requires_matching_root_identity(tmp_path: Path, stored_key: str) -> None:
    """A relative scan_root is current only when its path-free identity matches this checkout."""
    hook = _hook("inject-preamble")
    key = hook.root_key(tmp_path.resolve()) if stored_key == "matching" else stored_key
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"scan_root": ".", "scan_root_key": key}), encoding="utf-8")
    fields = hook.header_fields(index)
    assert fields["scan_root"] == "."
    assert fields["scan_root_key"] == key
    assert hook.index_matches_root(fields, tmp_path) == (stored_key == "matching")


def test_hook_project_key_matches_cli_identity(tmp_path: Path) -> None:
    """Hook telemetry joins CLI telemetry using the full path-free canonical-root digest."""
    hook = _hook("inject-preamble")
    assert hook._hookutil.project_key(tmp_path) == hook.root_key(tmp_path.resolve())
    assert len(hook._hookutil.project_key(tmp_path)) == 64


def test_scan_directory_without_uid_uses_private_user_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts without getuid retain a per-user child instead of reverting to shared sentinel names."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delattr(setup_scan_env.os, "getuid", raising=False)
    monkeypatch.setattr(setup_scan_env.getpass, "getuser", lambda: "example-user")
    directory = setup_scan_env._resolve_tmpdir()
    assert directory.parent == tmp_path
    assert directory.name == "codemap-scan-" + setup_scan_env.hashlib.sha256(b"example-user").hexdigest()[:16]
    assert directory.is_dir()


def test_safe_writer_rejects_foreign_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ordinary planted file owned by another user cannot be truncated by a hook."""
    hook = _hook("seed-session")
    marker = tmp_path / "marker"
    marker.write_text("untouched", encoding="utf-8")
    monkeypatch.setattr(hook.os, "getuid", lambda: marker.stat().st_uid + 1, raising=False)
    with pytest.raises(OSError, match="owned by another user"):
        hook._hookutil.write_sentinel(marker, "redirected")
    assert marker.read_text(encoding="utf-8") == "untouched"
