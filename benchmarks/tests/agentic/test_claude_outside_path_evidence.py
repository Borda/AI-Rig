"""Focused regression tests for Claude external-path telemetry.

The agentic runner records which paths an agent touched outside its disposable workspace. These tests feed it synthetic
stream events and check the recorded evidence and the containment decision, independent of the scoring host's OS.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest

_WORKSPACE = Path("/disposable/repository")


def _tool_events(tool_name: str, tool_input: dict[str, Any], result: str = "ok") -> list[dict[str, Any]]:
    """Pair one successful tool call with its result so both share a tool identifier.

    >>> request, response = _tool_events("Read", {"file_path": "example.py"})
    >>> call = request["message"]["content"][0]
    >>> result = response["message"]["content"][0]
    >>> call["input"], call["id"] == result["tool_use_id"], result["is_error"]
    ({'file_path': 'example.py'}, True, False)
    """
    return [
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "call", "name": tool_name, "input": tool_input}]},
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "call", "is_error": False, "content": result}]
            },
        },
    ]


class TestOutsideWorkspacePathEvidence:
    """Attempted and successful external accesses recorded for one tool call."""

    def test_ignores_launcher_shebang_in_written_content(self, script_run_agentic: Any) -> None:
        """A source-file payload does not masquerade as an external path access.

        Regression: successful writes containing a Python shebang quarantined Patch cells as if the agent had accessed
        the shebang launcher.
        """
        events = _tool_events(
            "Write",
            {"file_path": "/disposable/repository/test_fix.py", "content": "#!/usr/bin/env python\nprint('ok')\n"},
            result="",
        )

        attempted, successful = script_run_agentic._outside_workspace_path_evidence(events, _WORKSPACE)

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
    def test_records_successful_host_access(
        self, script_run_agentic: Any, tool_name: str, tool_input: dict[str, str]
    ) -> None:
        """Executable tool path fields and commands still record successful host access.

        A read, a write and a shell command naming a path outside the workspace each count as both attempted and
        successful access, so the cell stays quarantined.
        """
        attempted, successful = script_run_agentic._outside_workspace_path_evidence(
            _tool_events(tool_name, tool_input), _WORKSPACE
        )

        assert attempted == ["/host/source.py"]
        assert successful == ["/host/source.py"]

    @pytest.mark.parametrize(
        ("observed_path", "workspace_root", "expected_paths"),
        [
            pytest.param(
                "/host/source.py", PureWindowsPath(r"D:\agent\disposable"), ["/host/source.py"], id="windows-host-path"
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
    def test_decides_against_workspace_root_without_rerooting_path(
        self, script_run_agentic: Any, observed_path: str, workspace_root: Any, expected_paths: list[str]
    ) -> None:
        """Recorded evidence is the path the agent named, never one re-rooted on the scoring host.

        Regression: the observed path was pushed through ``Path.resolve()``. On Windows that gives a leading-slash path
        the current drive letter, so ``/host/source.py`` was reported as ``D:\\host\\source.py``: evidence about a file
        that was never touched. A checkout is never its own leak, and a neighbour whose name merely starts with the
        checkout's is outside it.
        """
        attempted, successful = script_run_agentic._outside_workspace_path_evidence(
            _tool_events("Read", {"file_path": observed_path}), workspace_root
        )

        assert attempted == expected_paths
        assert successful == expected_paths


class TestWorkspaceContainment:
    """Lexical containment of observed paths in a workspace root, free of host separators."""

    @pytest.mark.parametrize(
        ("workspace_root", "expected"),
        [
            pytest.param(PureWindowsPath(r"D:\agent\repo"), ("D:/agent/repo",), id="windows_backslash_root"),
            pytest.param(PurePosixPath("/agent/repo"), ("/agent/repo",), id="posix_root"),
        ],
    )
    def test_roots_are_separator_free(self, script_run_agentic: Any, workspace_root: Any, expected: tuple) -> None:
        """Root forms carry no host separator, so containment never depends on the scoring OS.

        A Windows backslash root and a POSIX root both normalize to forward-slash strings.
        """
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
    def test_drive_qualified_root_decides_membership_lexically(
        self, script_run_agentic: Any, observed: str, inside: bool
    ) -> None:
        """A drive-qualified checkout classifies observed paths without touching this filesystem.

        Members and the root itself are inside; a sibling sharing the name prefix and a path on another root are not.
        """
        roots = script_run_agentic._workspace_containment_roots(PureWindowsPath(r"D:\agent\repo"))

        assert script_run_agentic._is_inside_workspace(PurePosixPath(observed), roots) is inside
