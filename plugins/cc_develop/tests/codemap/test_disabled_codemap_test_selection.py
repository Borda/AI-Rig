"""Prove optional Codemap disabling never bypasses required heuristic test execution.

Each case extracts a shipped bash recipe from a skill document and runs it in a throwaway Git project whose one pytest
test records that it ran. The real shell, Git and pytest are the contract; only the Codemap provider is a logging stub.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from _develop_text import nested_bash_fences

ROOT = Path(__file__).resolve().parents[4]
BASH = shutil.which("bash")
_requires_bash_and_git = pytest.mark.skipif(
    BASH is None or shutil.which("git") is None, reason="shipped recipes require bash and local Git"
)


def _make_project(tmp_path: Path, test_passes: bool) -> Path:
    """Create a committed Git project with one changed source file and a name-matched test that records its run."""
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
    return project


def _install_stub_tools(tmp_path: Path) -> Path:
    """Put a logging ``codemap-py`` and a ``python`` shim on a private bin dir; return it."""
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
    return bin_dir


def _write_sentinels(tmp_path: Path, project: Path, skill: str, state: dict[str, str]) -> None:
    """Write the ``-disabled`` session sentinels the recipe reads, with *state* as ``dev-<skill>-<key>`` entries."""
    values = {
        "dev-pytest-cmd": "python -m pytest",
        f"dev-{skill}-dev-dir": str(project / "run"),
        "dev-codemap-currency": "off",
        "dev-fix-reused-pytest-cmd": "",
        "dev-batch-n": "2",
        "dev-batch-skill": skill,
        **{f"dev-{skill}-{key}": value for key, value in state.items()},
    }
    for prefix, value in values.items():
        (tmp_path / f"{prefix}-disabled").write_text(value + "\n", encoding="utf-8", newline="\n")


def _recipe_block(document: str, ordinal: int) -> str:
    """Return the *ordinal*-th bash fence of the skill document that calls ``dev_test_targets.py``."""
    source = (ROOT / "plugins/cc_develop/skills" / document).read_text(encoding="utf-8")
    return [block for block in nested_bash_fences(source) if 'bin/dev_test_targets.py"' in block][ordinal]


@pytest.fixture(name="run_recipe")
def _run_recipe(tmp_path: Path) -> Callable[..., tuple[subprocess.CompletedProcess[str], Path, Path]]:
    """Return a runner executing a recipe in a fresh project; it yields the result, the run marker and the query log."""

    def run(document: str, skill: str, ordinal: int, state: dict[str, str], test_passes: bool):
        """Run fence *ordinal* of *document* for *skill* with sentinel *state* and a passing/failing project test."""
        project = _make_project(tmp_path, test_passes)
        bin_dir = _install_stub_tools(tmp_path)
        _write_sentinels(tmp_path, project, skill, state)
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
        block = _recipe_block(document, ordinal)
        result = subprocess.run([BASH, "-c", block], cwd=project, env=env, capture_output=True, text=True, timeout=30)
        return result, marker, query_log

    return run


def _assert_heuristic_run(result: subprocess.CompletedProcess[str], marker: Path, query_log: Path, test_passes: bool):
    """Assert the name-matched test ran without provider calls and its failure propagated."""
    output = result.stdout + result.stderr
    payload = json.loads(result.stdout.splitlines()[0])
    assert marker.exists(), output
    assert marker.read_text() == "executed"
    assert not query_log.exists(), query_log.read_text() if query_log.exists() else ""
    assert payload["tests"] == ["tests/test_leaf.py"]
    assert payload["sources"] == {"tests/test_leaf.py": "heuristic"}
    assert payload["codemap"] == {"used": 0, "partial": 0, "fell_back": 0}
    assert result.returncode == (0 if test_passes else 1), output


@pytest.mark.integration
@_requires_bash_and_git
@pytest.mark.parametrize(
    ("skill", "ordinal", "state"),
    [
        pytest.param("feature", 0, {"codemap-enabled": "false"}, id="feature-affected-tests"),
        pytest.param("feature", 1, {"codemap-enabled": "false"}, id="feature-qa-rerun"),
        pytest.param("fix", 0, {"codemap-enabled": "false"}, id="fix-selection"),
        pytest.param("fix", 1, {"codemap-enabled": "false"}, id="fix-qa-rerun"),
        pytest.param("refactor", 0, {"codemap-enabled": "false"}, id="refactor-inner"),
        pytest.param("refactor", 1, {"codemap-enabled": "false"}, id="refactor-qa"),
        pytest.param("refactor", 0, {}, id="refactor-missing-state"),
    ],
)
@pytest.mark.parametrize("test_passes", [True, False])
def test_disabled_codemap_keeps_required_tests(run_recipe, skill, ordinal, state, test_passes):
    """Each exact recipe runs the name-matched pytest test without provider calls and propagates failure.

    With Codemap disabled (or its state missing) the recipe must fall back to the heuristic test file instead of
    skipping test selection, and a failing test must still fail the recipe.
    """
    result, marker, query_log = run_recipe(f"{skill}/SKILL.md", skill, ordinal, state, test_passes)

    _assert_heuristic_run(result, marker, query_log, test_passes)


@pytest.mark.integration
@_requires_bash_and_git
@pytest.mark.parametrize("test_passes", [True, False])
def test_disabled_codemap_keeps_required_tests_in_shared_batch(run_recipe, test_passes):
    """The shared batch recipe runs the heuristic test with Codemap disabled and reports the run exit.

    Batch mode loads its run fence from ``_shared/batch-mode.md``; the fence must print ``BATCH_RUN_EXIT`` matching the
    test outcome so the caller knows whether to bisect.
    """
    result, marker, query_log = run_recipe(
        "_shared/batch-mode.md", "feature", 0, {"codemap-enabled": "false"}, test_passes
    )

    _assert_heuristic_run(result, marker, query_log, test_passes)
    assert f"BATCH_RUN_EXIT={0 if test_passes else 1}" in result.stdout
