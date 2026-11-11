"""Exercise whole-change test selection through one batch and host-specific command rendering."""

from __future__ import annotations

import importlib
import json
import shlex
import subprocess
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


@pytest.fixture(name="union_helper")
def _union_helper() -> ModuleType:
    """Load the shipped helper through the same bin path used by the skill."""
    return importlib.import_module("test_impact_union")


@pytest.fixture(name="diff_file")
def _diff_file(tmp_path: Path) -> Path:
    """Retain the full changed-symbol input while exposing repeated targets and unmapped files."""
    path = tmp_path / "diff.json"
    path.write_text(
        json.dumps(
            {
                "changed_modules": [
                    {"module": "pkg.a", "changed_symbols": ["pkg.a::run", "pkg.a::run"]},
                    {"module": "pkg.b", "changed_symbols": []},
                ],
                "unmapped_files": ["new.py"],
                "test_impact": {"test_files": ["tests/mock_only.py"]},
                "index": {"query_complete": True, "stale": False},
            }
        ),
        encoding="utf-8",
    )
    return path


def _batch_payload() -> dict:
    """Supply overlapping tests plus one target's incomplete-index caveat from the engine boundary."""
    complete = {"query_complete": True, "stale": False}
    return {
        "batch": [
            {
                "ok": True,
                "result": {
                    "test_files": ["tests/a space.py"],
                    "index": {**complete, "hint": "confirm local callbacks"},
                },
            },
            {"ok": True, "result": {"test_files": ["tests/a space.py", "tests/it's.py"], "index": complete}},
            {
                "ok": True,
                "result": {
                    "test_files": ["tests/b.py"],
                    "index": {"query_complete": False, "stale": True, "completeness_reason": "missing calls"},
                },
            },
        ],
        "count": 3,
        "index": {"query_complete": False, "stale": True},
    }


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_union_uses_one_batch_and_host_quoting(
    union_helper: ModuleType,
    diff_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    """Every distinct module and changed symbol contributes without mock-only tests or duplicate calls."""
    launcher = str(diff_file.parent / "launcher")
    which = Mock(return_value=launcher)
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(_batch_payload()), ""))
    monkeypatch.setattr(union_helper.shutil, "which", which)
    monkeypatch.setattr(union_helper.subprocess, "run", runner)
    monkeypatch.setattr(union_helper.sys, "platform", platform)

    rc = union_helper.main([str(diff_file)])

    output = capsys.readouterr()
    assert rc == 0, output.err
    summary = json.loads(output.out)
    which.assert_called_once_with("codemap-py")
    runner.assert_called_once()
    assert runner.call_args.args[0] == [launcher, "query", "batch", "-"]
    assert json.loads(runner.call_args.kwargs["input"]) == [
        {"cmd": "test-impact", "args": ["pkg.a", "--no-mocks"]},
        {"cmd": "test-impact", "args": ["pkg.a::run", "--no-mocks"]},
        {"cmd": "test-impact", "args": ["pkg.b", "--no-mocks"]},
    ]
    files = ["tests/a space.py", "tests/b.py", "tests/it's.py"]
    assert summary["test_impact"]["test_files"] == files
    assert summary["test_impact"]["total"] == 3
    assert summary["unmapped_files"] == ["new.py"]
    assert summary["index"] == {"query_complete": False, "stale": True}
    assert summary["target_caveats"] == [
        {
            "target": "pkg.a",
            "index": {"query_complete": True, "stale": False, "hint": "confirm local callbacks"},
        },
        {
            "target": "pkg.b",
            "index": {"query_complete": False, "stale": True, "completeness_reason": "missing calls"},
        },
    ]
    assert summary["diff_index"] == {"query_complete": True, "stale": False}
    expected = {
        "linux": shlex.join(["pytest", *files]),
        "win32": "pytest 'tests/a space.py' 'tests/b.py' 'tests/it''s.py'",
    }
    assert summary["test_impact"]["pytest_cmd"] == expected[platform]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"batch": [{"ok": False, "error": "query failed"}]}, id="failed-item"),
        pytest.param({"batch": []}, id="missing-items"),
        pytest.param({"batch": [{"ok": True, "result": {}}] * 3}, id="missing-test-list"),
        pytest.param({"error": "batch rejected"}, id="error-payload"),
    ],
)
def test_failed_batch_never_prints_empty_union(
    union_helper: ModuleType,
    diff_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict,
) -> None:
    """A clean process exit cannot hide a failed, missing, or malformed batch item."""
    monkeypatch.setattr(union_helper.shutil, "which", Mock(return_value="launcher"))
    monkeypatch.setattr(
        union_helper.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), ""))
    )

    assert union_helper.main([str(diff_file)]) == 1

    output = capsys.readouterr()
    assert not output.out
    assert "!" in output.err


def test_missing_launcher_reports_failure(
    union_helper: ModuleType, diff_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Launcher lookup must fail explicitly rather than execute a shell or report no tests."""
    monkeypatch.setattr(union_helper.shutil, "which", Mock(return_value=None))

    assert union_helper.main([str(diff_file)]) == 1

    output = capsys.readouterr()
    assert "codemap-py" in output.err
    assert not output.out
