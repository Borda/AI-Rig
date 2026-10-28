"""Check that local review patch context is independent of Git hunk settings."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_local_review_diff_ignores_interhunk_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A local hunk-merging setting must not change the frozen review patch."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.delenv("GIT_DIFF_OPTS", raising=False)
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=repository, capture_output=True, check=True)

    git("init", "-q")
    git("config", "user.name", "Review fixture")
    git("config", "user.email", "review@example.invalid")
    source = repository / "module.py"
    lines = [f"line {number}" for number in range(30)]
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    git("add", source.name)
    git("commit", "-qm", "base")
    lines[3] = "first change"
    lines[13] = "second change"
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    collector = Path(__file__).resolve().parents[2] / "shared" / "collect_diff.py"
    first = tmp_path / "first"
    second = tmp_path / "second"
    subprocess.run([sys.executable, str(collector), "--out", str(first)], cwd=repository, check=True)
    git("config", "diff.interHunkContext", "10")
    subprocess.run([sys.executable, str(collector), "--out", str(second)], cwd=repository, check=True)

    original = (first / "diff.patch").read_bytes()
    assert original.count(b"@@ -") == 2
    assert (second / "diff.patch").read_bytes() == original


def test_local_review_diff_ignores_diff_algorithm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured diff algorithm must not change a frozen local review patch."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.delenv("GIT_DIFF_OPTS", raising=False)
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=repository, capture_output=True, check=True)

    git("init", "-q")
    git("config", "user.name", "Review fixture")
    git("config", "user.email", "review@example.invalid")
    original = "4 1 1 2 3 4 4 1 4 4 2 4 3 3 2 0 2 3 2 4 3 0 4 3 2 2 4 1 0 0 2 2 1 0 2 0 2 1 4 2".split()
    source = repository / "module.py"
    source.write_text("\n".join(original) + "\n", encoding="utf-8")
    git("add", source.name)
    git("commit", "-qm", "base")
    changed = "4 3 1 1 2 3 1 4 4 3 2 4 3 3 2 0 2 3 3 1 4 3 0 4 2 2 2 4 1 0 2 1 0 2 0 2 2 1 3 2".split()
    source.write_text("\n".join(changed) + "\n", encoding="utf-8")
    collector = Path(__file__).resolve().parents[2] / "shared" / "collect_diff.py"
    first = tmp_path / "first"
    second = tmp_path / "second"
    git("config", "diff.algorithm", "myers")
    subprocess.run([sys.executable, str(collector), "--out", str(first)], cwd=repository, check=True)
    git("config", "diff.algorithm", "histogram")
    subprocess.run([sys.executable, str(collector), "--out", str(second)], cwd=repository, check=True)

    assert (second / "diff.patch").read_bytes() == (first / "diff.patch").read_bytes()
