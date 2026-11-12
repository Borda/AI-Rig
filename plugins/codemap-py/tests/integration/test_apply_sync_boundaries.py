"""Exercise untrusted integration plans and source-write boundaries."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from _host_skips import skip_file_symlink_unavailable
from codemap_py import integration
from codemap_py.integration import apply_sync


@pytest.fixture
def source_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a consumer checkout with native and Git probes isolated from the host."""
    manifest = tmp_path / "plugins/cc_oss/.claude-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "oss", "version": "1.0.0"}), encoding="utf-8")
    monkeypatch.setattr(integration.index_paths, "canonical_root", lambda: tmp_path)
    monkeypatch.setattr(integration.native, "_native_json_probe", lambda argv: None)
    monkeypatch.setattr(apply_sync, "_git_dirty", lambda root, path: False)
    return tmp_path


@pytest.mark.parametrize("field", ["path", "new_block"])
def test_forged_source_plan_is_refused(source_repo: Path, field: str) -> None:
    """Recomputing approval cannot authorize an arbitrary file or managed block."""
    plan = integration.build_plan("claude", ["oss"], None, source_repo)
    plan["ops"][0][field] = "plugins/cc_oss/README.md" if field == "path" else "injected prose\n"
    plan["plan_sha256"] = integration.compute_plan_sha256(plan)

    with pytest.raises(integration.RefusalError, match="planner"):
        integration.apply_plan(plan, plan["plan_sha256"], source_repo)

    assert not (source_repo / plan["ops"][0]["path"]).exists()


def test_source_plan_remains_valid_after_planning_second(source_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Timestamp metadata does not invalidate a genuine delayed approval."""
    monkeypatch.setattr("codemap_py.integration.managed_block._utc_now_iso", lambda: "2025-01-01T00:00:00Z")
    plan = integration.build_plan("claude", ["oss"], None, source_repo)
    monkeypatch.setattr("codemap_py.integration.managed_block._utc_now_iso", lambda: "2025-01-02T00:00:00Z")

    result = integration.apply_plan(plan, plan["plan_sha256"], source_repo)

    assert result["state"] == "complete"
    assert (source_repo / plan["ops"][0]["path"]).read_text(encoding="utf-8") == plan["ops"][0]["new_block"]


@pytest.mark.parametrize("role", ["marketplace", "plugin"])
def test_forged_runtime_argv_is_refused(source_repo: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    """A correctly hashed plan cannot execute commands outside native planner output."""
    plan = integration.build_plan("claude", ["oss"], "local-candidate", source_repo)
    op = next(op for op in plan["ops"] if op.get("role") == role)
    op["argv"] = [["arbitrary-executable", "payload"]]
    plan["ops"] = [op]
    plan["plan_sha256"] = integration.compute_plan_sha256(plan)
    calls = []
    monkeypatch.setattr(integration.native, "_run_native_required", calls.append)

    with pytest.raises(integration.RefusalError, match="planner"):
        integration.sync_plan(plan, plan["plan_sha256"], "local-candidate", source_repo)

    assert calls == []


@pytest.mark.parametrize("index", ["../../escaped", True, -1, None])
def test_before_image_refuses_noninteger_or_negative_index(tmp_path: Path, index: object) -> None:
    """Journal names cannot be steered outside the before-image directory."""
    journal = integration.Journal(tmp_path / "journal")

    with pytest.raises(integration.IntegrationError, match="index"):
        journal.save_before_image(index, b"original")

    assert not (tmp_path / "escaped.bak").exists()


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(OSError("missing git"), id="missing-git"),
        pytest.param(subprocess.TimeoutExpired("git", 5), id="git-timeout"),
    ],
)
def test_git_probe_failure_is_dirty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    """An unavailable dirty-state probe cannot authorize an overlay."""

    def fail(*args, **kwargs):
        """Simulate a failed external Git process."""
        raise failure

    monkeypatch.setattr(apply_sync.subprocess, "run", fail)
    assert apply_sync._git_dirty(tmp_path, "target.md") is True


def test_nonzero_git_probe_is_dirty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Git errors with empty stdout still refuse overlays."""
    monkeypatch.setattr(
        apply_sync.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 128, "", "fatal")
    )
    assert apply_sync._git_dirty(tmp_path, "target.md") is True


@skip_file_symlink_unavailable
def test_symlinked_source_parent_is_refused(source_repo: Path) -> None:
    """An intermediate consumer-directory link never redirects the source write."""
    plan = integration.build_plan("claude", ["oss"], None, source_repo)
    outside = source_repo / "outside"
    outside.mkdir()
    (source_repo / "plugins/cc_oss/skills").symlink_to(outside, target_is_directory=True)

    with pytest.raises(integration.RefusalError):
        integration.apply_plan(plan, plan["plan_sha256"], source_repo)

    assert not (outside / "_shared/codemap-gates.md").exists()


@skip_file_symlink_unavailable
def test_atomic_write_refuses_symlink_parent(tmp_path: Path) -> None:
    """A parent replaced with a symlink after validation cannot receive the bytes."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "parent").symlink_to(outside, target_is_directory=True)

    with pytest.raises(integration.RefusalError):
        apply_sync._atomic_write(tmp_path / "parent/target.md", "payload")

    assert not (outside / "target.md").exists()


@skip_file_symlink_unavailable
@pytest.mark.skipif(
    apply_sync.os.open not in apply_sync.os.supports_dir_fd,
    reason="directory-relative writes are unavailable on this host",
)
def test_parent_swap_during_replace_does_not_write_through_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory swap at replacement either refuses or writes only through the held descriptor."""
    parent = tmp_path / "parent"
    parent.mkdir()
    moved = tmp_path / "moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = parent / "target.md"
    real_replace = apply_sync.os.replace

    def swap_then_replace(src, dst, **kwargs):
        """Swap the parent at the final filesystem mutation boundary."""
        parent.rename(moved)
        parent.symlink_to(outside, target_is_directory=True)
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(apply_sync.os, "replace", swap_then_replace)

    with pytest.raises(integration.RefusalError):
        apply_sync._atomic_write(target, "payload")

    assert not (outside / "target.md").exists()


def test_atomic_write_without_directory_fd_keeps_lf_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The portable path preserves exact bytes on hosts without directory-relative APIs."""
    monkeypatch.setattr(apply_sync.os, "supports_dir_fd", set())
    target = tmp_path / "nested/target.md"

    apply_sync._atomic_write(target, "first\nsecond\n")

    assert target.read_bytes() == b"first\nsecond\n"


@skip_file_symlink_unavailable
@pytest.mark.parametrize("restore", [False, True])
def test_rollback_refuses_symlinked_parent(source_repo: Path, restore: bool) -> None:
    """Neither deletion nor before-image restoration may follow a redirected rollback parent."""
    plan = integration.build_plan("claude", ["oss"], None, source_repo)
    journal = integration.Journal(source_repo / "journal")
    _seed_before_image(journal, restore)
    target = source_repo / plan["ops"][0]["path"]
    target.parent.parent.mkdir(parents=True)
    outside = source_repo / "outside"
    outside.mkdir()
    victim = outside / target.name
    victim.write_bytes(b"foreign")
    target.parent.symlink_to(outside, target_is_directory=True)

    state = apply_sync._rollback_source_writes(plan["ops"], source_repo, journal)

    assert state == "rollback-failed"
    assert victim.read_bytes() == b"foreign"


def _seed_before_image(journal: integration.Journal, restore: bool) -> None:
    """Seed a before-image only for the existing-file rollback scenario."""
    if restore:
        journal.save_before_image(0, b"original")
