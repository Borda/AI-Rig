"""Focused regression tests for Claude external-path telemetry."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest


def test_external_path_evidence_ignores_a_launcher_shebang_in_written_content(script_run_agentic: Any) -> None:
    """A source-file payload must not masquerade as an external path access.

    Regression: successful writes containing a Python shebang quarantined Patch
    cells as if the agent had accessed the shebang launcher.
    """
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "write",
                        "name": "Write",
                        "input": {
                            "file_path": "/disposable/repository/test_fix.py",
                            "content": "#!/usr/bin/env python\nprint('ok')\n",
                        },
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "write", "is_error": False, "content": ""}]},
        },
    ]

    attempted, successful = script_run_agentic._outside_workspace_path_evidence(events, Path("/disposable/repository"))

    assert attempted == []
    assert successful == []


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        pytest.param("Read", {"file_path": "/host/source.py"}, id="external_read"),
        pytest.param("Write", {"file_path": "/host/source.py", "content": "pass\n"}, id="external_write"),
        pytest.param("Bash", {"command": "cat /host/source.py"}, id="external_command"),
    ],
)
def test_external_path_evidence_keeps_successful_host_access_quarantined(
    script_run_agentic: Any, tool_name: str, tool_input: dict[str, str]
) -> None:
    """Executable tool path fields and commands still record successful host access."""
    events = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "external", "name": tool_name, "input": tool_input}]},
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "external", "is_error": False, "content": "ok"}]
            },
        },
    ]

    attempted, successful = script_run_agentic._outside_workspace_path_evidence(events, Path("/disposable/repository"))

    assert attempted == ["/host/source.py"]
    assert successful == ["/host/source.py"]


def _read_events(file_path: str) -> list[dict[str, Any]]:
    """Pair a Read request for the supplied path with a successful result sharing its tool identifier.

    >>> request, response = _read_events("example.py")
    >>> call = request["message"]["content"][0]
    >>> result = response["message"]["content"][0]
    >>> call["input"], call["id"] == result["tool_use_id"], result["is_error"]
    ({'file_path': 'example.py'}, True, False)
    """
    return [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "read", "name": "Read", "input": {"file_path": file_path}}]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "read", "is_error": False, "content": "ok"}]
            },
        },
    ]


@pytest.mark.parametrize(
    ("observed_path", "workspace_root", "expected_paths"),
    [
        pytest.param(
            "/host/source.py",
            PureWindowsPath(r"D:\agent\disposable"),
            ["/host/source.py"],
            id="simulated-windows-host-path",
        ),
        pytest.param(
            "/disposable/repository/src/app.py",
            PurePosixPath("/disposable/repository"),
            [],
            id="workspace-member-is-not-external",
        ),
        pytest.param(
            "/disposable/repository-backup/src/app.py",
            PurePosixPath("/disposable/repository"),
            ["/disposable/repository-backup/src/app.py"],
            id="prefix-sibling-stays-external",
        ),
    ],
)
def test_external_path_evidence_is_decided_against_the_workspace_root(
    script_run_agentic: Any, observed_path: str, workspace_root: Any, expected_paths: list[str]
) -> None:
    """Recorded evidence is the path the agent named, never one re-rooted on the scoring host.

    Regression: the observed path was pushed through ``Path.resolve()``. On Windows that gives a leading-slash path the
    current drive letter, so ``/host/source.py`` was reported as ``D:\\host\\source.py``: evidence about a file that was
    never touched. A checkout is never its own leak, and a neighbour whose name merely starts with the checkout's is
    outside it.
    """
    attempted, successful = script_run_agentic._outside_workspace_path_evidence(
        _read_events(observed_path), workspace_root
    )

    assert attempted == expected_paths
    assert successful == expected_paths


@pytest.mark.parametrize(
    ("workspace_root", "expected"),
    [
        pytest.param(PureWindowsPath(r"D:\agent\repo"), ("D:/agent/repo",), id="windows_backslash_root"),
        pytest.param(PurePosixPath("/agent/repo"), ("/agent/repo",), id="posix_root"),
    ],
)
def test_containment_roots_are_separator_free(script_run_agentic: Any, workspace_root: Any, expected: tuple) -> None:
    """Root forms carry no host separator, so containment never depends on the scoring OS."""
    roots = script_run_agentic._workspace_containment_roots(workspace_root)

    assert tuple(str(root) for root in roots) == expected


@pytest.mark.parametrize(
    ("observed", "inside"),
    [
        pytest.param("D:/agent/repo/src/app.py", True, id="member"),
        pytest.param("D:/agent/repo", True, id="root_itself"),
        pytest.param("D:/agent/repo-backup/src/app.py", False, id="prefix_sibling"),
        pytest.param("/host/source.py", False, id="foreign_root"),
    ],
)
def test_simulated_windows_rooted_containment_is_decided_lexically(
    script_run_agentic: Any, observed: str, inside: bool
) -> None:
    """A drive-qualified checkout classifies observed paths without touching this filesystem."""
    roots = script_run_agentic._workspace_containment_roots(PureWindowsPath(r"D:\agent\repo"))

    assert script_run_agentic._is_inside_workspace(PurePosixPath(observed), roots) is inside
