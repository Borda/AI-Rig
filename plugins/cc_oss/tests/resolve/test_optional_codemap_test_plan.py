"""Execute owner-state handoff into optional Codemap selection without suppressing required tests."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import resolve_test_plan

ROOT = Path(__file__).resolve().parents[4]
BASH = shutil.which("bash")


def test_codemap_uses_the_resolved_windows_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider discovered via PATHEXT must be executed by its resolved path."""
    executable = str(tmp_path / "codemap-py.cmd")
    calls = []
    monkeypatch.setattr(resolve_test_plan.shutil, "which", lambda name: executable if name == "codemap-py" else None)

    def run(args: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
        calls.append((args, cwd, timeout))
        return 0, '{"test_files": ["tests/test_leaf.py"], "index": {"query_complete": true}}'

    monkeypatch.setattr(resolve_test_plan, "_run", run)
    assert resolve_test_plan.codemap_tests("leaf", tmp_path) == ["tests/test_leaf.py"]
    assert calls == [([executable, "query", "test-impact", "leaf"], tmp_path, 120)]


def test_batch_query_rejects_shell_metacharacters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A filename-derived module cannot inject commands into a Windows batch launcher."""
    monkeypatch.setattr(resolve_test_plan.shutil, "which", lambda name: r"C:\Tools\codemap-py.cmd")
    calls = []
    monkeypatch.setattr(resolve_test_plan, "_run", lambda args, cwd, timeout: calls.append(args) or (0, "{}"))
    assert resolve_test_plan.codemap_tests("pkg.x&echo injected", tmp_path) is None
    assert calls == []


def test_relative_launcher_is_anchored_before_changing_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PATH entry relative to the caller remains valid when the query runs at the repo root."""
    caller = tmp_path / "subdir"
    caller.mkdir()
    monkeypatch.chdir(caller)
    monkeypatch.setattr(resolve_test_plan.shutil, "which", lambda name: "bin/codemap-py")
    calls = []
    monkeypatch.setattr(resolve_test_plan, "_run", lambda args, cwd, timeout: calls.append(args) or (0, "{}"))
    resolve_test_plan.codemap_tests("pkg.mod", tmp_path)
    assert calls[0][0] == str(caller / "bin" / "codemap-py")


@pytest.mark.integration
@pytest.mark.skipif(BASH is None or shutil.which("git") is None, reason="consumer commands require bash and local Git")
@pytest.mark.parametrize(
    ("relative", "prefix"),
    [
        pytest.param("review/templates/agent-prompts.md", "review", id="review-qa"),
        pytest.param("resolve/modes/dispatch-phase2.md", "resolve", id="resolve-implementation"),
        pytest.param("resolve/modes/lint-qa-gate.md", "resolve", id="resolve-qa"),
    ],
)
@pytest.mark.parametrize("test_passes", [True, False])
def test_disabled_lookup_keeps_executable_heuristic_plan(
    tmp_path: Path, relative: str, prefix: str, test_passes: bool
) -> None:
    """An exact caller environment prefix disables only lookup; its returned test command really executes."""
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    (project / "leaf.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests = project / "tests"
    tests.mkdir()
    marker = tmp_path / "ran"
    (tests / "test_leaf.py").write_text(
        "import os\nfrom pathlib import Path\n\ndef test_leaf():\n"
        "    Path(os.environ['TEST_MARKER']).write_text('executed')\n"
        f"    assert {test_passes!r}\n",
        encoding="utf-8",
        newline="\n",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    query_log = tmp_path / "queries"
    provider_script = bin_dir / "provider.py"
    provider_script.write_text(
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['QUERY_LOG']).write_text('queried')\n"
        'print(\'{"test_files": [], "index": {"query_complete": true}}\')\n',
        encoding="utf-8",
        newline="\n",
    )
    provider = bin_dir / "codemap-py"
    provider.write_text(
        f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "{provider_script.as_posix()}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    provider.chmod(0o700)
    if os.name == "nt":
        (bin_dir / "codemap-py.cmd").write_text(
            f'@"{sys.executable}" "{provider_script}" %*\r\n', encoding="utf-8", newline=""
        )
    for executable, args in (("python", ""), ("pytest", "-m pytest ")):
        path = bin_dir / executable
        path.write_text(
            f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" {args}"$@"\n', encoding="utf-8", newline="\n"
        )
        path.chmod(0o700)
    (tmp_path / f"{prefix}-codemap-enabled-owner").write_text("false\n", encoding="utf-8", newline="\n")
    source = (ROOT / "plugins/cc_oss/skills" / relative).read_text(encoding="utf-8").replace('\\"', '"')
    command = re.search(r'CODEMAP_ENABLED=\$\(cat .*?\) python "[^"\n]+/resolve_test_plan.py" targeted', source)
    assert command is not None, "caller must pass its persisted choice into the mixed-purpose helper"
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "TMPDIR": str(tmp_path),
        "CSID": "owner",
        "CLAUDE_PLUGIN_ROOT": str(ROOT / "plugins/cc_oss"),
        "TEST_MARKER": str(marker),
        "QUERY_LOG": str(query_log),
        "PYTEST_ADDOPTS": "-p no:xdist -p no:cacheprovider",
    }
    env.pop("CODEMAP_ENABLED", None)
    result = subprocess.run(
        [BASH, "-c", command.group() + " --files leaf.py"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["tests"] == ["tests/test_leaf.py"]
    assert plan["sources"] == {"tests/test_leaf.py": "heuristic"}
    assert plan["codemap"] == {"used": 0, "fell_back": 0}
    assert not query_log.exists()
    tested = subprocess.run(
        [BASH, "-c", plan["command"]], cwd=project, env=env, capture_output=True, text=True, timeout=30
    )
    assert marker.exists(), tested.stdout + tested.stderr
    assert marker.read_text(encoding="utf-8") == "executed"
    assert tested.returncode == (0 if test_passes else 1), tested.stdout + tested.stderr

    # The same helper without an owning invocation environment retains its ordinary standalone behavior.
    standalone = subprocess.run(
        [sys.executable, str(ROOT / "plugins/cc_oss/bin/resolve_test_plan.py"), "targeted", "--files", "leaf.py"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert standalone.returncode == 0, standalone.stderr
    assert query_log.read_text() == "queried"
    assert json.loads(standalone.stdout)["codemap"] == {"used": 1, "fell_back": 0}
