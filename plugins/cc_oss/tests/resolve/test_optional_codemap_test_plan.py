"""Execute owner-state handoff into optional Codemap selection without suppressing required tests.

The launcher tests mock ``resolve_test_plan._run`` and ``shutil.which``; the integration tests run the consumer command
from the shipped skill files under real ``bash`` and ``git`` because the executed command is the contract there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest
import resolve_test_plan

ROOT = Path(__file__).resolve().parents[4]
BASH = shutil.which("bash")

_TEST_IMPACT = '{"test_files": ["tests/test_leaf.py"], "index": {"query_complete": true}}'
_CONSUMER_COMMAND = re.compile(r'CODEMAP_ENABLED=\$\(cat .*?\) python "[^"\n]+/resolve_test_plan.py" targeted')


@dataclass(frozen=True)
class _Toolchain:
    """Fake ``PATH`` toolchain: environment for the subprocesses plus the files that prove what ran."""

    env: dict[str, str]
    marker: Path
    query_log: Path


def _write_executable(path: Path, text: str) -> None:
    """Write an LF-only shell shim and make it executable."""
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o700)


def _write_shims(bin_dir: Path, provider_script: Path) -> None:
    """Install ``codemap-py``, ``python`` and ``pytest`` shims (plus the Windows launcher) into ``bin_dir``."""
    python = Path(sys.executable).as_posix()
    _write_executable(bin_dir / "codemap-py", f'#!/bin/sh\nexec "{python}" "{provider_script.as_posix()}" "$@"\n')
    _write_executable(bin_dir / "python", f'#!/bin/sh\nexec "{python}" "$@"\n')
    _write_executable(bin_dir / "pytest", f'#!/bin/sh\nexec "{python}" -m pytest "$@"\n')
    if os.name == "nt":
        (bin_dir / "codemap-py.cmd").write_text(
            f'@"{sys.executable}" "{provider_script}" %*\r\n', encoding="utf-8", newline=""
        )


def _consumer_command(relative: str) -> str:
    """Return the ``resolve_test_plan.py targeted`` command a shipped skill file tells its caller to run."""
    source = (ROOT / "plugins/cc_oss/skills" / relative).read_text(encoding="utf-8").replace('\\"', '"')
    match = next((m for line in source.splitlines() if (m := _CONSUMER_COMMAND.search(line))), None)
    assert match is not None, "caller must pass its persisted choice into the mixed-purpose helper"
    return match.group()


def _bash(command: str, project: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run ``command`` under ``bash -c`` in ``project`` with ``env``."""
    return subprocess.run([BASH, "-c", command], cwd=project, env=env, capture_output=True, text=True, timeout=30)


@pytest.fixture
def toolchain(tmp_path: Path) -> _Toolchain:
    """Build a ``PATH`` whose ``codemap-py`` records that it was queried and prints an empty selection."""
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
    _write_shims(bin_dir, provider_script)
    marker = tmp_path / "ran"
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
    return _Toolchain(env=env, marker=marker, query_log=query_log)


@pytest.fixture
def make_project(tmp_path: Path):
    """Return a factory for a git project whose ``tests/test_leaf.py`` records its run and passes or fails."""

    def make(test_passes: bool) -> Path:
        """Create the project; ``test_passes`` decides whether its one test passes."""
        project = tmp_path / "project"
        (project / "tests").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
        (project / "leaf.py").write_text("VALUE = 1\n", encoding="utf-8")
        (project / "tests" / "test_leaf.py").write_text(
            "import os\nfrom pathlib import Path\n\ndef test_leaf():\n"
            "    Path(os.environ['TEST_MARKER']).write_text('executed')\n"
            f"    assert {test_passes!r}\n",
            encoding="utf-8",
            newline="\n",
        )
        return project

    return make


class TestCodemapLauncher:
    """How ``resolve_test_plan.codemap_tests`` locates and invokes the Codemap provider."""

    @mock.patch("resolve_test_plan._run", autospec=True, return_value=(0, _TEST_IMPACT))
    @mock.patch("resolve_test_plan.shutil.which", autospec=True)
    def test_uses_the_resolved_windows_launcher(
        self, mock_which: mock.MagicMock, mock_run: mock.MagicMock, tmp_path: Path
    ) -> None:
        """A provider discovered via PATHEXT must be executed by its resolved path.

        ``which`` returns ``codemap-py.cmd`` on Windows; running the bare name would miss the batch launcher, so the
        query must start with the path ``which`` resolved. Only the process boundary is mocked.
        """
        executable = str(tmp_path / "codemap-py.cmd")
        mock_which.side_effect = lambda name, *_: executable if name == "codemap-py" else None

        assert resolve_test_plan.codemap_tests("leaf", tmp_path) == ["tests/test_leaf.py"]

        mock_run.assert_called_once_with([executable, "query", "test-impact", "leaf"], tmp_path, 120)

    @mock.patch("resolve_test_plan._run", autospec=True, return_value=(0, "{}"))
    @mock.patch("resolve_test_plan.shutil.which", autospec=True, return_value=r"C:\Tools\codemap-py.cmd")
    def test_batch_query_rejects_shell_metacharacters(
        self, mock_which: mock.MagicMock, mock_run: mock.MagicMock, tmp_path: Path
    ) -> None:
        """A filename-derived module cannot inject commands into a Windows batch launcher.

        A module name carrying ``&`` would be parsed by ``cmd.exe`` as a second command, so the lookup gives up without
        spawning the provider.
        """
        assert resolve_test_plan.codemap_tests("pkg.x&echo injected", tmp_path) is None
        mock_run.assert_not_called()

    @mock.patch("resolve_test_plan._run", autospec=True, return_value=(0, "{}"))
    @mock.patch("resolve_test_plan.shutil.which", autospec=True, return_value="bin/codemap-py")
    def test_relative_launcher_is_anchored_before_changing_cwd(
        self, mock_which: mock.MagicMock, mock_run: mock.MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A PATH entry relative to the caller remains valid when the query runs at the repo root.

        The caller sits in ``subdir`` and finds ``bin/codemap-py`` relative to it; the query runs elsewhere, so the
        launcher path is made absolute against the caller's directory first.
        """
        caller = tmp_path / "subdir"
        caller.mkdir()
        monkeypatch.chdir(caller)

        resolve_test_plan.codemap_tests("pkg.mod", tmp_path)

        assert mock_run.call_args_list[0].args[0][0] == str(caller / "bin" / "codemap-py")


@pytest.mark.integration
@pytest.mark.skipif(BASH is None or shutil.which("git") is None, reason="consumer commands require bash and local Git")
class TestDisabledLookup:
    """The shipped skill commands, run under real bash and git, with the provider lookup switched off by its owner."""

    @pytest.mark.parametrize(
        ("relative", "prefix"),
        [
            pytest.param("review/templates/agent-prompts.md", "review", id="review-qa"),
            pytest.param("resolve/modes/dispatch-phase2.md", "resolve", id="resolve-implementation"),
            pytest.param("resolve/modes/lint-qa-gate.md", "resolve", id="resolve-qa"),
        ],
    )
    @pytest.mark.parametrize("test_passes", [True, False])
    def test_keeps_executable_heuristic_plan(
        self, tmp_path: Path, toolchain: _Toolchain, make_project, relative: str, prefix: str, test_passes: bool
    ) -> None:
        """An exact caller environment prefix disables only lookup; its returned test command really executes.

        The owner recorded ``false`` for its Codemap choice, so the helper must skip the provider query yet still return
        a heuristic plan whose command runs the project's real test and reports its pass or fail status.
        """
        project = make_project(test_passes)
        (tmp_path / f"{prefix}-codemap-enabled-owner").write_text("false\n", encoding="utf-8", newline="\n")

        planned = _bash(_consumer_command(relative) + " --files leaf.py", project, toolchain.env)
        assert planned.returncode == 0, planned.stderr
        plan = json.loads(planned.stdout)
        tested = _bash(plan["command"], project, toolchain.env)

        assert plan["tests"] == ["tests/test_leaf.py"]
        assert plan["sources"] == {"tests/test_leaf.py": "heuristic"}
        assert plan["codemap"] == {"used": 0, "fell_back": 0}
        assert not toolchain.query_log.exists()
        assert toolchain.marker.read_text(encoding="utf-8") == "executed", tested.stdout + tested.stderr
        assert tested.returncode == (0 if test_passes else 1), tested.stdout + tested.stderr

    def test_standalone_helper_still_queries_the_provider(
        self, tmp_path: Path, toolchain: _Toolchain, make_project
    ) -> None:
        """The same helper without an owning invocation environment retains its ordinary standalone behavior.

        Only the owner's recorded choice may switch the lookup off; a bare run of the script must still ask Codemap.
        """
        project = make_project(True)
        helper = ROOT / "plugins/cc_oss/bin/resolve_test_plan.py"

        standalone = subprocess.run(
            [sys.executable, str(helper), "targeted", "--files", "leaf.py"],
            cwd=project,
            env=toolchain.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert standalone.returncode == 0, standalone.stderr
        assert toolchain.query_log.read_text() == "queried"
        assert json.loads(standalone.stdout)["codemap"] == {"used": 1, "fell_back": 0}
