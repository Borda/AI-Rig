"""Verify session-wide Git configuration prevents maintenance races in copied fixtures."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def maintenance_conftest() -> ModuleType:
    """Load the root conftest by path without resolving a nested conftest of the same name."""
    path = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("git_maintenance_conftest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_session_environment_disables_maintenance() -> None:
    """Expose a complete Git environment config entry before any test fixtures run.

    Git subprocesses inherit the pytest process environment. A missing or incomplete indexed entry would leave fixture
    commits free to start detached maintenance while another fixture copies the repository.
    """
    count = int(os.environ["GIT_CONFIG_COUNT"])
    entries = {
        os.environ[f"GIT_CONFIG_KEY_{index}"].lower(): os.environ[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }

    assert count >= 1
    assert entries["maintenance.auto"] == "false"


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        pytest.param(
            {},
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false"},
            id="no-config",
        ),
        pytest.param(
            {"GIT_CONFIG_COUNT": "0", "PATH": "unchanged"},
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "maintenance.auto",
                "GIT_CONFIG_VALUE_0": "false",
                "PATH": "unchanged",
            },
            id="explicit-zero",
        ),
        pytest.param(
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.autocrlf", "GIT_CONFIG_VALUE_0": "input"},
            {
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "core.autocrlf",
                "GIT_CONFIG_VALUE_0": "input",
                "GIT_CONFIG_KEY_1": "maintenance.auto",
                "GIT_CONFIG_VALUE_1": "false",
            },
            id="preserve-existing-config",
        ),
        pytest.param(
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "true"},
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false"},
            id="reuse-existing-maintenance-entry",
        ),
        pytest.param(
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "Maintenance.Auto", "GIT_CONFIG_VALUE_0": "true"},
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "Maintenance.Auto", "GIT_CONFIG_VALUE_0": "false"},
            id="case-insensitive-git-key",
        ),
    ],
)
def test_preserves_config_without_mutating_input(
    maintenance_conftest: ModuleType, existing: dict[str, str], expected: dict[str, str]
) -> None:
    """Preserve caller config and keep repeated imports from appending duplicate keys.

    A parent runner may already supply indexed Git config or enable maintenance explicitly. Updating a copy must retain
    unrelated entries, disable maintenance, and produce the same mapping when applied again by an xdist worker.
    """
    original = existing.copy()

    result = maintenance_conftest.disable_git_maintenance(existing)

    assert result == expected
    assert existing == original
    assert maintenance_conftest.disable_git_maintenance(result) == expected


@pytest.mark.integration
@pytest.mark.parametrize(
    ("options", "expected_maintenance"),
    [
        pytest.param([], False, id="inherited-session-config"),
        pytest.param(["-c", "maintenance.auto=true"], True, id="enabled-maintenance-control"),
    ],
)
def test_commit_trace_has_no_maintenance_child(tmp_path: Path, options: list[str], expected_maintenance: bool) -> None:
    """Observe maintenance suppression on a real empty commit and validate the trace detector.

    The normal commit inherits session config with no per-call maintenance override. An explicitly enabled control must
    spawn maintenance, proving that the negative trace assertion can detect the original race's trigger.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    trace = tmp_path / "trace.jsonl"
    environment = {
        **os.environ,
        "GIT_TRACE2_EVENT": str(trace),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }

    subprocess.run(
        ["git", *options, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "fixture"],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
    )

    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert any(event.get("event") == "exit" and event.get("code") == 0 for event in events)
    assert (
        any(event.get("event") == "child_start" and "maintenance" in event.get("argv", []) for event in events)
        is expected_maintenance
    )
