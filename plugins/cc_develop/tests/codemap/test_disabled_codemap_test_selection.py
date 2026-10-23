"""Prove optional Codemap disabling never bypasses required heuristic test execution."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
BASH = shutil.which("bash")


@pytest.mark.integration
@pytest.mark.skipif(BASH is None or shutil.which("git") is None, reason="shipped recipes require bash and local Git")
@pytest.mark.parametrize(
    ("skill", "ordinal", "enabled"),
    [
        pytest.param("feature", 0, "false", id="feature-affected-tests"),
        pytest.param("feature", 1, "false", id="feature-qa-rerun"),
        pytest.param("fix", 0, "false", id="fix-selection"),
        pytest.param("fix", 1, "false", id="fix-qa-rerun"),
        pytest.param("refactor", 0, "false", id="refactor-inner"),
        pytest.param("refactor", 1, "false", id="refactor-qa"),
        pytest.param("refactor", 0, None, id="refactor-missing-state"),
        pytest.param("batch", 0, "false", id="shared-batch"),
    ],
)
@pytest.mark.parametrize("test_passes", [True, False])
def test_disabled_codemap_keeps_required_tests(
    tmp_path: Path, skill: str, ordinal: int, enabled: str | None, test_passes: bool
) -> None:
    """Each exact recipe runs the name-matched pytest test without provider calls and propagates failure."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "leaf.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests = project / "tests"
    tests.mkdir()
    (tests / "test_leaf.py").write_text(
        "import os\nfrom pathlib import Path\n\ndef test_leaf():\n"
        "    Path(os.environ['TEST_MARKER']).write_text('executed')\n"
        f"    assert {test_passes!r}\n",
        encoding="utf-8",
        newline="\n",
    )
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
    ):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    (project / "leaf.py").write_text("VALUE = 2\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    provider = bin_dir / "codemap-py"
    provider.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        "with open(os.environ['QUERY_LOG'],'a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "print(json.dumps({'test_files': [], 'index': {'query_complete': True}}))\n",
        encoding="utf-8",
        newline="\n",
    )
    provider.chmod(0o700)
    python = bin_dir / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8", newline="\n")
    python.chmod(0o700)
    actual_skill = "feature" if skill == "batch" else skill
    values = {
        "dev-pytest-cmd": "python -m pytest",
        f"dev-{actual_skill}-dev-dir": str(project / "run"),
        "dev-codemap-currency": "off",
        "dev-fix-reused-pytest-cmd": "",
        "dev-batch-n": "2",
        "dev-batch-skill": actual_skill,
    }
    if enabled is not None:
        values[f"dev-{actual_skill}-codemap-enabled"] = enabled
    for prefix, value in values.items():
        (tmp_path / f"{prefix}-disabled").write_text(value + "\n", encoding="utf-8")
    relative = "_shared/batch-mode.md" if skill == "batch" else f"{skill}/SKILL.md"
    source = (ROOT / "plugins/cc_develop/skills" / relative).read_text(encoding="utf-8")
    blocks = [
        m.group(2)
        for m in re.finditer(r"(?m)^([ \t]*)```bash\n(.*?)\n\1```", source, re.S)
        if 'bin/dev_test_targets.py"' in m.group(2)
    ]
    block = blocks[ordinal]
    query_log, marker = tmp_path / "queries.jsonl", tmp_path / "test-ran"
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "TMPDIR": str(tmp_path),
        "CLAUDE_CODE_SESSION_ID": "disabled",
        "CSID": "disabled",
        "CLAUDE_PLUGIN_ROOT": str(ROOT / "plugins/cc_develop"),
        "QUERY_LOG": str(query_log),
        "TEST_MARKER": str(marker),
        "PYTEST_ADDOPTS": "-p no:xdist -p no:cacheprovider",
    }
    env.pop("CODEMAP_ENABLED", None)
    result = subprocess.run([BASH, "-c", block], cwd=project, env=env, capture_output=True, text=True, timeout=30)
    assert marker.exists(), result.stdout + result.stderr
    assert marker.read_text() == "executed"
    assert not query_log.exists(), query_log.read_text() if query_log.exists() else ""
    payload = json.loads(result.stdout.splitlines()[0])
    assert payload["tests"] == ["tests/test_leaf.py"]
    assert payload["sources"] == {"tests/test_leaf.py": "heuristic"}
    assert payload["codemap"] == {"used": 0, "partial": 0, "fell_back": 0}
    assert result.returncode == (0 if test_passes else 1), result.stdout + result.stderr
    if skill == "batch":
        assert f"BATCH_RUN_EXIT={0 if test_passes else 1}" in result.stdout
