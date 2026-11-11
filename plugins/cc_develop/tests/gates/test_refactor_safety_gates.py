"""Exercise the refactor skill's pre-edit, team characterization and installation gates."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _develop_text import nested_bash_fences

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/refactor/SKILL.md"
_TEAM = _PLUGIN / "skills/refactor/modes/team-mode.md"
_BASH = shutil.which("bash")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


def _block(path: Path, marker: str) -> str:
    """Extract one shipped shell gate by a distinguishing marker."""
    return next(block for block in nested_bash_fences(path.read_text(encoding="utf-8")) if marker in block)


def _quality_stack_blocks() -> str:
    """Extract the quality-stack entry flow, including any prechecks before the loader."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("**Quality-stack"))
    end = next(i for i, line in enumerate(lines) if line.startswith("**Bounded independent implementation challenge**"))
    return "\n".join(nested_bash_fences("\n".join(lines[start:end])))


def _run(block: str, tmp_path: Path, plugin: Path = _PLUGIN) -> subprocess.CompletedProcess[str]:
    """Run the shipped block with isolated session state and the existing interpreter."""
    env = {
        **os.environ,
        "TMPDIR": tmp_path.as_posix(),
        "CLAUDE_CODE_SESSION_ID": "refactor-safety",
        "CLAUDE_PLUGIN_ROOT": plugin.as_posix(),
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    return subprocess.run([_BASH, "-c", block], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)


@_skip_no_bash
@pytest.mark.parametrize("gate", ["T1", "T2"])
@pytest.mark.parametrize(
    ("body", "exit_code", "verdict"),
    [
        pytest.param("def test_current():\n    assert True\n", 0, "GATE OK", id="green"),
        pytest.param("def test_current():\n    assert False\n", 1, "GATE FAIL", id="red"),
        pytest.param("value = 1\n", 5, "GATE FAIL", id="no-tests"),
        pytest.param("def broken(\n", 2, "GATE FAIL", id="collection-error"),
    ],
)
def test_team_gate_runs_the_saved_characterization_file(
    tmp_path: Path, gate: str, body: str, exit_code: int, verdict: str
) -> None:
    """Both team gates execute real pytest and refuse failing or empty safety nets."""
    target = tmp_path / "tests/test_current.py"
    target.parent.mkdir()
    target.write_text(body, encoding="utf-8")
    (tmp_path / "dev-refactor-char-test-refactor-safety").write_text("tests/test_current.py\n", encoding="utf-8")
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("python -m pytest\n", encoding="utf-8")
    result = _run(_block(_TEAM, f"Gate {gate}"), tmp_path)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert verdict in result.stdout


@_skip_no_bash
@pytest.mark.parametrize("gate", ["T1", "T2"])
def test_team_gate_refuses_untrusted_runner(tmp_path: Path, gate: str) -> None:
    """A command planted in runner state cannot execute or become a green gate."""
    target = tmp_path / "tests/test_current.py"
    target.parent.mkdir()
    target.write_text("def test_current():\n    assert True\n", encoding="utf-8")
    (tmp_path / "dev-refactor-char-test-refactor-safety").write_text("tests/test_current.py\n", encoding="utf-8")
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("touch injected\n", encoding="utf-8")
    result = _run(_block(_TEAM, f"Gate {gate}"), tmp_path)
    assert result.returncode == 64, result.stdout + result.stderr
    assert "GATE ERROR" in result.stdout
    assert not (tmp_path / "injected").exists()


@_skip_no_bash
@pytest.mark.parametrize("gate", ["T1", "T2"])
def test_team_gate_missing_characterization_path_blocks(tmp_path: Path, gate: str) -> None:
    """Lost characterization state blocks before any test command can run."""
    result = _run(_block(_TEAM, f"Gate {gate}"), tmp_path)
    assert result.returncode != 0
    assert "BLOCKED" in result.stdout
    assert "GATE OK" not in result.stdout


@_skip_no_bash
def test_missing_quality_stack_blocks(tmp_path: Path) -> None:
    """A missing shipped quality stack is an installation error, never a successful skip."""
    (tmp_path / "shared").mkdir()
    (tmp_path / "dev-shared-refactor-safety").write_text((tmp_path / "shared").as_posix() + "\n", encoding="utf-8")
    result = _run(_quality_stack_blocks(), tmp_path)
    assert result.returncode != 0
    assert "! BREAKING" in result.stdout
    assert "quality stack skipped" not in result.stdout


def test_baseline_precedes_team_and_solo_source_edits() -> None:
    """The same scoped selector establishes a completed green baseline before either execution branch."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    baseline = next(i for i, line in enumerate(lines) if line.startswith("**Pre-edit baseline**"))
    batch = next(i for i, line in enumerate(lines) if line.startswith("**Batch mode gate**"))
    assert lines.index("## Step 4: Refactor with safety net") < baseline < batch
    team_lines = _TEAM.read_text(encoding="utf-8").splitlines()
    baseline_call = next(i for i, line in enumerate(team_lines) if line.startswith("Before T2, the parent executes"))
    spawn = next(i for i, line in enumerate(team_lines) if line.startswith("**Step T2"))
    assert baseline_call < spawn
    block = _block(_SKILL, "BASELINE_EXIT")
    assert 'bin/dev_test_targets.py" --skill refactor --files' in block
    assert '--check-record "$DEV_DIR/test-targets.jsonl" --expect-exit 0' in block
    assert "/develop:fix" in block
    assert "exit 1" in block


@_skip_no_bash
@pytest.mark.parametrize(
    ("selector_exit", "tests", "expected_exit", "message"),
    [
        pytest.param(0, ["test_scope.py"], 0, "BASELINE OK", id="completed-green"),
        pytest.param(1, ["test_scope.py"], 1, "/develop:fix", id="red-baseline"),
        pytest.param(0, [], 1, "no completed nonempty baseline", id="empty-selection"),
        pytest.param(2, ["test_scope.py"], 2, "runner or collection failed", id="old-green-record-new-run-error"),
    ],
)
def test_baseline_requires_a_completed_nonempty_green_run(
    tmp_path: Path, selector_exit: int, tests: list[str], expected_exit: int, message: str
) -> None:
    """The shell gate rejects red, empty and failed-selection runs even when an older green record exists."""
    plugin = tmp_path / "plugin"
    scripts = plugin / "bin"
    scripts.mkdir(parents=True)
    (tmp_path / "dev-refactor-dev-dir-refactor-safety").write_text(tmp_path.as_posix() + "\n", encoding="utf-8")
    (tmp_path / "test-targets.jsonl").write_text(
        json.dumps({"tests": tests, "exit": 1 if selector_exit == 1 else 0}) + "\n", encoding="utf-8"
    )
    (scripts / "dev_test_targets.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(_PLUGIN / 'bin')!r})\n"
        "from dev_test_targets import main\n"
        "if '--check-record' in sys.argv:\n"
        "    raise SystemExit(main())\n"
        f"raise SystemExit({selector_exit})\n",
        encoding="utf-8",
    )
    block = _block(_SKILL, "BASELINE_EXIT").replace("'<scoped_python_files>'", shlex.quote("pkg/module.py"))
    result = _run(block, tmp_path, plugin)
    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert message in result.stdout
    assert ("BASELINE OK" in result.stdout) == (expected_exit == 0)


def test_failed_edit_reverses_only_owned_changes() -> None:
    """Unbatched rollback must preserve pre-existing dirty edits and concurrent owners."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    rollback = next(line for line in lines if line.startswith("4. Tests fail:"))
    assert "reverse only your own Edit" in rollback
    assert "never" in rollback
    assert "git checkout" in rollback
    assert "git restore" in rollback
    assert "pre-existing" in rollback
    assert "concurrent" in rollback


def test_team_prompt_loads_the_implementation_obligations() -> None:
    """The implementation teammate reads loop caps, rollback policy and local review before editing."""
    lines = _TEAM.read_text(encoding="utf-8").splitlines()
    prompt = next(line for line in lines if line.startswith("You are a foundry:sw-engineer teammate"))
    assert "skills/refactor/SKILL.md" in prompt
    assert "Step 4: Refactor with safety net" in prompt
    assert "Step 5: Review and close gaps" in prompt
    assert "5" in prompt
    assert "1800" in prompt
    assert "targeted" in prompt


@_skip_no_bash
@pytest.mark.parametrize("gate", ["T1", "T2"])
@pytest.mark.parametrize("path", ["test_outside.py", "tests/../test_outside.py"])
def test_team_gate_refuses_paths_outside_tests(tmp_path: Path, gate: str, path: str) -> None:
    """An existing file outside tests cannot become the team's characterization proof."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "test_outside.py").write_text("def test_external():\n    assert True\n", encoding="utf-8")
    (tmp_path / "dev-refactor-char-test-refactor-safety").write_text(path + "\n", encoding="utf-8")
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("python -m pytest\n", encoding="utf-8")
    result = _run(_block(_TEAM, f"Gate {gate}"), tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "! BLOCKED" in result.stdout
    assert "GATE OK" not in result.stdout


@_skip_no_bash
@pytest.mark.parametrize("path", ["tests/test_quote's.py", "tests/test_$(touch injected).py"])
def test_characterization_path_is_persisted_literally(tmp_path: Path, path: str) -> None:
    """A teammate path preserves quotes and shell metacharacters through persistence and execution."""
    target = tmp_path / path
    target.parent.mkdir()
    target.write_text("def test_current():\n    assert True\n", encoding="utf-8")
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("python -m pytest\n", encoding="utf-8")
    block = _block(_TEAM, "<char_test_file>").replace("<char_test_file>", path)
    saved = _run(block, tmp_path)
    assert saved.returncode == 0, saved.stderr
    assert (tmp_path / "dev-refactor-char-test-refactor-safety").read_text().strip() == path
    result = _run(_block(_TEAM, "Gate T1"), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GATE OK" in result.stdout
    assert not (tmp_path / "injected").exists()


@_skip_no_bash
@pytest.mark.parametrize("present", [True, False])
def test_teammate_skill_resolution_checks_the_installed_file(tmp_path: Path, present: bool) -> None:
    """The producing block prints an absolute existing skill path or blocks before dispatch."""
    plugin = tmp_path / "installed plugin"
    skill = plugin / "skills/refactor/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill", encoding="utf-8")
    plugin_arg = plugin if present else tmp_path / "missing"
    result = _run(_block(_TEAM, "REFACTOR_SKILL="), tmp_path, plugin_arg)
    assert result.returncode == (0 if present else 1), result.stdout + result.stderr
    assert (skill.as_posix() in result.stdout) is present
    assert ("! BLOCKED" in result.stdout) is not present


@_skip_no_bash
def test_quality_stack_cold_loads_without_shared_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing warm sentinel cannot veto an available installed quality stack."""
    monkeypatch.setenv("HOME", tmp_path.as_posix())
    shared = tmp_path / ".claude/plugins/cache/borda-ai-rig/develop/1.0.0/skills/_shared"
    shared.mkdir(parents=True)
    (shared / "foundry--quality-stack.md").write_text("installed quality stack\n", encoding="utf-8")
    (shared / "codex-delegation.md").write_text("installed delegation contract\n", encoding="utf-8")
    result = _run(_quality_stack_blocks(), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "installed quality stack" in result.stdout
    assert "! BLOCKED" not in result.stdout
    sibling = _block(_PLUGIN / "skills/_shared/foundry--quality-stack.md", 'cat "$_SHARED/codex-delegation.md"')
    loaded = _run(sibling, tmp_path)
    assert loaded.returncode == 0, loaded.stdout + loaded.stderr
    assert "installed delegation contract" in loaded.stdout


@_skip_no_bash
def test_baseline_placeholder_blocks_before_selector(tmp_path: Path) -> None:
    """An unsubstituted scope cannot be diagnosed as a source failure."""
    (tmp_path / "dev-refactor-dev-dir-refactor-safety").write_text(tmp_path.as_posix() + "\n", encoding="utf-8")
    result = _run(_block(_SKILL, "BASELINE_EXIT"), tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "! BLOCKED" in result.stdout
    assert "placeholder" in result.stdout
    assert "/develop:fix" not in result.stdout


@_skip_no_bash
@pytest.mark.parametrize("record_exit", [None, 0])
def test_baseline_exit_one_without_completed_record_is_runner_failure(tmp_path: Path, record_exit: int | None) -> None:
    """A crashed runner must not send the user to repair nonexistent source failures."""
    plugin = tmp_path / "plugin"
    scripts = plugin / "bin"
    scripts.mkdir(parents=True)
    (tmp_path / "dev-refactor-dev-dir-refactor-safety").write_text(tmp_path.as_posix() + "\n", encoding="utf-8")
    (tmp_path / "test-targets.jsonl").write_text(
        json.dumps({"tests": ["tests/test_existing.py"], "exit": record_exit}) + "\n", encoding="utf-8"
    )
    (scripts / "dev_test_targets.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    block = _block(_SKILL, "BASELINE_EXIT").replace("<scoped_python_files>", "pkg/module.py")
    result = _run(block, tmp_path, plugin)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "runner" in result.stdout
    assert "/develop:fix" not in result.stdout


@_skip_no_bash
@pytest.mark.parametrize(
    ("existing_passes", "expected_exit", "message"),
    [
        pytest.param(True, 0, "BASELINE OK", id="remaining-tests-green"),
        pytest.param(False, 1, "pre-existing test failure", id="remaining-tests-red"),
    ],
)
def test_accepted_red_characterization_is_excluded_from_baseline_and_t2(
    tmp_path: Path, existing_passes: bool, expected_exit: int, message: str
) -> None:
    """Accepted bad assertions are excluded while real source-test failures still block the baseline."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_red.py").write_text("def test_red():\n    assert False\n", encoding="utf-8")
    (tests / "test_existing.py").write_text(f"def test_existing():\n    assert {existing_passes!r}\n", encoding="utf-8")
    (tmp_path / "accepted-red-characterization.txt").write_text("tests/test_red.py\n", encoding="utf-8")
    (tmp_path / "dev-refactor-dev-dir-refactor-safety").write_text(tmp_path.as_posix() + "\n", encoding="utf-8")
    (tmp_path / "dev-refactor-char-test-refactor-safety").write_text("tests/test_red.py\n", encoding="utf-8")
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("python -m pytest\n", encoding="utf-8")
    plugin = tmp_path / "plugin"
    scripts = plugin / "bin"
    scripts.mkdir(parents=True)
    (scripts / "dev_test_targets.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(_PLUGIN / 'bin')!r})\n"
        "import dev_test_targets as selector\n"
        "if '--files' in sys.argv and '--run' not in sys.argv:\n"
        "    print(json.dumps({'tests': ['tests/test_red.py', 'tests/test_existing.py']}))\n"
        "    raise SystemExit(0)\n"
        "selector._repo_root = Path.cwd\n"
        "raise SystemExit(selector.main())\n",
        encoding="utf-8",
    )
    block = _block(_SKILL, "BASELINE_EXIT").replace(
        "'<scoped_python_files>'", shlex.join(["tests/test_red.py", "tests/test_existing.py"])
    )
    result = _run(block, tmp_path, plugin)
    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert "accepted-red characterization test excluded" in result.stdout
    assert message in result.stdout
    assert ("BASELINE OK" in result.stdout) is existing_passes
    record = json.loads((tmp_path / "test-targets.jsonl").read_text().splitlines()[-1])
    assert record["tests"] == ["tests/test_existing.py"]
    assert record["exit"] == expected_exit
    post = _run(_block(_TEAM, "Gate T2"), tmp_path)
    assert post.returncode == 0, post.stdout + post.stderr
    assert "accepted-red characterization test excluded" in post.stdout
    assert "GATE OK" not in post.stdout
