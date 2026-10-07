"""Verify forbidden path detection and its pre-commit integration."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "benchmarks" / "check-portable-paths.py"


@pytest.fixture(name="checker", scope="module")
def _checker() -> ModuleType:
    """Import the portability checker without running its command-line scan.

    >>> getfixture("checker").__name__
    'check_portable_paths'
    """
    spec = importlib.util.spec_from_file_location("check_portable_paths", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("filename", "source", "expected_violation"),
    [
        pytest.param(
            "source.py",
            'LOCK = "/private/tmp/frozen/index.json"\n',
            "/private/tmp/frozen/index.json",
            id="python-private-tmp",
        ),
        pytest.param("source.py", 'HOME = "/Users/alice/project"\n', "/Users/alice/project", id="python-personal-home"),
        pytest.param("source.py", 'TMP = "/tmp/runtime/state"\n', "/tmp/runtime/state", id="python-tmp"),
        pytest.param(
            "source.sh", 'ROOT="/private/tmp/benchmark"\n', 'ROOT="/private/tmp/benchmark"', id="shell-assignment"
        ),
        pytest.param(
            "policy.json",
            '{"validation_worktree": "/private/tmp/benchmark"}\n',
            '{"validation_worktree": "/private/tmp/benchmark"}',
            id="json-policy",
        ),
        pytest.param(
            "README.md",
            "Run the benchmark from `/Users/alice/project`.\n",
            "Run the benchmark from `/Users/alice/project`.",
            id="markdown-machine-path",
        ),
    ],
)
def test_machine_bound_literals_are_rejected(
    checker: ModuleType, tmp_path: Path, filename: str, source: str, expected_violation: str
) -> None:
    """Machine-bound literals fail with their exact line and value.

    Python literals report the offending path value; executable shell assignments receive the same temporary-path guard,
    committed JSON policy cannot reintroduce a machine-specific worktree, and current benchmark documentation cannot
    publish a machine-specific command, so those report the whole offending line.
    """
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")

    assert checker.find_violations(path) == [(1, expected_violation)]


def test_comments_placeholders_and_relative_paths_pass(checker: ModuleType, tmp_path: Path) -> None:
    """Comments, documented placeholders, and relative runtime paths remain portable."""
    path = tmp_path / "source.py"
    path.write_text(
        '# Historical /private/tmp/example is prose only.\nROOT = "/Users/<name>/project"\nCACHE = "benchmarks/.cache"\n',
        encoding="utf-8",
    )

    assert checker.find_violations(path) == []


def test_main_reports_violation_and_fails(
    checker: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pre-commit entry point fails closed and identifies the offending line."""
    path = tmp_path / "source.py"
    path.write_text('ROOT = "/private/tmp/benchmark"\n', encoding="utf-8")

    assert checker.main([str(path)]) == 1
    assert capsys.readouterr().err == (
        f"{path}:1: hardcoded absolute machine or temporary path: /private/tmp/benchmark\n"
    )


def test_precommit_runs_portability_checker_on_governed_files() -> None:
    """The regression checker must remain wired into the commit gate."""
    config: dict[str, Any] = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [hook for repo in config["repos"] if repo["repo"] == "local" for hook in repo["hooks"]]
    portability = next(hook for hook in hooks if hook["id"] == "check-benchmark-portable-paths")

    assert portability["entry"] == "python3 benchmarks/check-portable-paths.py"
    governed = re.compile(portability["files"])
    assert governed.search("benchmarks/run-all.sh")
    assert governed.search("benchmarks/README.md")
    assert governed.search("benchmarks/policy/provider-parity-methodology.json")
    assert not governed.search("benchmarks/manifests/codex-agentic.json")
    assert not governed.search("benchmarks/results/run/telemetry.jsonl")
    assert not governed.search("benchmarks/tests/test_fixture.py")
