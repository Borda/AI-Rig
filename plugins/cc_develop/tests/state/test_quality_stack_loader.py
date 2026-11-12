"""Verify the shared loader publishes the same directory to quality-stack sibling fences."""

from __future__ import annotations

from pathlib import Path

import dev_load_shared
import pytest
from dev_run_dir import read_sentinel, write_sentinel


def test_quality_stack_load_persists_selected_shared_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """The emitted document and subsequent quality-stack reads use the same warm resolution."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "quality-loader")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = tmp_path / ".claude/plugins/cache/borda-ai-rig/develop/1.0.0"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name":"develop"}', encoding="utf-8")
    shared = root / "skills/_shared"
    shared.mkdir(parents=True)
    payload = b"# Shared Quality Stack\r\n"
    (shared / "foundry--quality-stack.md").write_bytes(payload)
    assert write_sentinel("dev-shared", str(shared)) is not None
    assert dev_load_shared.main(["--qs-dir", "foundry--quality-stack.md"]) == 0
    assert read_sentinel("qs-shared-dir") == str(shared)
    assert capsysbinary.readouterr().out == payload


def test_failed_quality_stack_load_does_not_publish_new_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing required stack must fail before creating a misleading ready-to-use directory sentinel."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "missing-quality")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = tmp_path / ".claude/plugins/cache/borda-ai-rig/develop/1.0.0"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name":"develop"}', encoding="utf-8")
    shared = root / "skills/_shared"
    shared.mkdir(parents=True)
    assert write_sentinel("dev-shared", str(shared)) is not None
    assert dev_load_shared.main(["--qs-dir", "foundry--quality-stack.md"]) == 1
    assert read_sentinel("qs-shared-dir") == ""
