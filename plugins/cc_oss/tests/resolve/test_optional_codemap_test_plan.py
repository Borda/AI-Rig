"""Execute owner-state handoff into optional Codemap selection without suppressing required tests."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
BASH = shutil.which("bash")


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
    provider = bin_dir / "codemap-py"
    provider.write_text(
        f"#!{sys.executable}\nimport os\nfrom pathlib import Path\n"
        "Path(os.environ['QUERY_LOG']).write_text('queried')\n"
        'print(\'{"test_files": [], "index": {"query_complete": true}}\')\n',
        encoding="utf-8",
        newline="\n",
    )
    provider.chmod(0o700)
    for executable, args in (("python", ""), ("pytest", "-m pytest ")):
        path = bin_dir / executable
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" {args}"$@"\n', encoding="utf-8", newline="\n")
        path.chmod(0o700)
    (tmp_path / f"{prefix}-codemap-enabled-owner").write_text("false\n", encoding="utf-8")
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
        shlex.split(plan["command"]), cwd=project, env=env, capture_output=True, text=True, timeout=30
    )
    assert marker.read_text() == "executed"
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
