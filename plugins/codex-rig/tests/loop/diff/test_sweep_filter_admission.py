"""Prevent parent Git readers from executing configured clean or process filters."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from _modules import load_module_registered

SHARED = Path(__file__).resolve().parents[3] / "shared"


@pytest.mark.integration
@pytest.mark.parametrize("filter_kind", ["clean", "process"])
def test_gate_source_inspection_rejects_executable_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filter_kind: str
) -> None:
    """Gate source inspection returns a coded rejection before status can execute a content filter."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.syspath_prepend(str(SHARED))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "source.txt").write_text("before\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("*.txt filter=hostile\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    marker = tmp_path / "executed"
    script = tmp_path / "filter.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    command = shlex.join([Path(sys.executable).as_posix(), script.as_posix()])
    subprocess.run(["git", "-C", str(tmp_path), "config", f"filter.hostile.{filter_kind}", command], check=True)
    (tmp_path / "source.txt").write_text("after\n", encoding="utf-8")
    gates = load_module_registered("sweep_filter_run_gates", SHARED / "run_gates.py")
    receipt = gates.inspect_source(tmp_path.resolve())
    assert "git-executable-filter-forbidden" in receipt.get("error", "")
    assert not marker.exists()


@pytest.mark.integration
@pytest.mark.parametrize("filter_kind", ["clean", "process", "smudge"])
@pytest.mark.parametrize("consumer", ["collector", "pilot"])
def test_parent_content_reads_reject_executable_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filter_kind: str, consumer: str
) -> None:
    """Reject configured executable transforms before the command can create its marker."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    repository = tmp_path / "source"
    subprocess.run(["git", "init", "-q", str(repository)], check=True, capture_output=True)
    marker = tmp_path / "executed"
    script = tmp_path / "filter.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    command = shlex.join([Path(sys.executable).as_posix(), script.as_posix()])
    subprocess.run(["git", "-C", str(repository), "config", f"filter.hostile.{filter_kind}", command], check=True)
    (repository / ".gitattributes").write_text("*.txt filter=hostile\n", encoding="utf-8")
    (repository / "source.txt").write_text("source\n", encoding="utf-8")
    collector = load_module_registered("sweep_filter_collect_diff", SHARED / "collect_diff.py")
    pilot = load_module_registered("sweep_filter_parallel_worktrees", SHARED / "parallel_worktrees.py")
    readers = {
        "collector": lambda: collector.collect_diff("working-tree", "", tmp_path / "output"),
        "pilot": lambda: pilot._git_filtered_oid(repository, "source.txt"),
    }
    monkeypatch.chdir(repository)
    with pytest.raises((RuntimeError, pilot.PilotError), match="git-executable-filter-forbidden"):
        readers[consumer]()
    assert not marker.exists()


@pytest.mark.integration
def test_builtin_line_ending_filtering_remains_admitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Built-in newline normalization does not require an executable filter."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitattributes").write_text("*.txt text eol=lf\n", encoding="utf-8")
    (tmp_path / "source.txt").write_bytes(b"source\r\n")
    pilot = load_module_registered("sweep_filter_builtin_pilot", SHARED / "parallel_worktrees.py")
    expected = (
        subprocess.run(
            ["git", "-C", str(tmp_path), "hash-object", "--stdin"], input=b"source\n", capture_output=True, check=True
        )
        .stdout.decode()
        .strip()
    )
    assert pilot._git_filtered_oid(tmp_path, "source.txt") == expected


@pytest.mark.integration
def test_index_attributes_cannot_hide_an_executable_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Checkout admission checks indexed attributes even when the working copy removes its filter declaration."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "source.txt").write_text("source\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("*.txt filter=hostile\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    (tmp_path / ".gitattributes").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "config", "filter.hostile.process", "external-filter"], check=True)
    collector = load_module_registered("sweep_filter_index", SHARED / "collect_diff.py")
    with pytest.raises(RuntimeError, match="git-executable-filter-forbidden"):
        collector.reject_executable_filters(tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("filter_kind", ["clean", "process", "smudge"])
def test_head_filter_attributes_survive_staged_deletions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filter_kind: str
) -> None:
    """A checkout cannot execute HEAD filters hidden by staged deletion of attributes and their target file."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "source.txt").write_text("source\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("*.txt filter=hostile\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "rm", ".gitattributes", "source.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", f"filter.hostile.{filter_kind}", "external-filter"], check=True
    )
    index_before = (tmp_path / ".git" / "index").read_bytes()
    collector = load_module_registered("sweep_filter_deleted_head", SHARED / "collect_diff.py")
    with pytest.raises(RuntimeError, match="git-executable-filter-forbidden"):
        collector.reject_executable_filters(tmp_path)
    assert (tmp_path / ".git" / "index").read_bytes() == index_before
    assert not (tmp_path / ".gitattributes").exists()
    assert not (tmp_path / "source.txt").exists()
