"""Keep configured remote names in Git's operand position during receipt verification."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from _modules import load_module_registered

SHARED = Path(__file__).resolve().parents[2] / "shared"


def test_remediation_remote_name_is_an_operand(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured option-shaped remote cannot become a remote get-url option."""
    monkeypatch.syspath_prepend(str(SHARED))
    module = load_module_registered("sweep_remediation_remote", SHARED / "remediation_branch.py")
    calls = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Model Git's operand boundary while returning one configured tracking destination."""
        calls.append(argv)
        answers = {
            ("check-ref-format", "refs/heads/main"): "",
            ("config", "--get", "branch.child.merge"): "refs/heads/main",
            ("config", "--get", "branch.child.remote"): "--fork",
            ("remote",): "--fork",
            ("remote", "get-url", "--all", "--", "--fork"): "https://github.com/example/project.git",
        }
        answer = answers.get(tuple(argv[1:]))
        return subprocess.CompletedProcess(argv, 2 if answer is None else 0, answer or "", "")

    monkeypatch.setattr(subprocess, "run", run)
    module._destination("child", "main", "example/project", "https://github.com/example/project/pull/7", 7)
    assert calls[-1] == ["git", "remote", "get-url", "--all", "--", "--fork"]
