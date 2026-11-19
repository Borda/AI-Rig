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
from _develop_shell_plugin import render_shell, shell_path
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
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        [_BASH, "-c", render_shell(block, Path(env.get("CLAUDE_PLUGIN_ROOT", str(_PLUGIN))), "develop")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def _scope_findings(run_dir: Path, text: str = "none · 0 findings\n", plugin: Path | None = None) -> None:
    """Record the Step 1 findings file in ``run_dir`` and point the run-dir sentinel at it.

    A fake ``plugin`` gets the real findings helper, since every gate now validates the file before trusting a run.
    """
    (run_dir / "scope-findings.md").write_text(text, encoding="utf-8")
    (run_dir / "dev-refactor-dev-dir-refactor-safety").write_text(
        run_dir.as_posix() + "\n", encoding="utf-8", newline="\n"
    )
    if plugin is not None:
        shutil.copy(_PLUGIN / "bin/dev_scope_findings.py", plugin / "bin/dev_scope_findings.py")


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
    (tmp_path / "dev-refactor-char-test-refactor-safety").write_text(
        "tests/test_current.py\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("python -m pytest\n", encoding="utf-8", newline="\n")
    _scope_findings(tmp_path)
    result = _run(_block(_TEAM, f"Gate {gate}"), tmp_path)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert verdict in result.stdout


@_skip_no_bash
@pytest.mark.parametrize(
    ("findings", "test_map", "exit_code", "verdict"),
    [
        pytest.param("none · 0 findings\n", None, 0, "GATE OK", id="no-special-cases"),
        pytest.param("F1 · special-case · pkg/mod.py::f · retry\n", None, 3, "F1 (unmapped)", id="unmapped"),
        pytest.param(
            "F1 · special-case · pkg/mod.py::f · retry\n",
            "F1 · tests/test_current.py::test_current\n",
            0,
            "GATE OK",
            id="mapped-green",
        ),
        pytest.param(
            "F1 · special-case · pkg/mod.py::f · retry\n",
            "F1 · tests/test_pin.py::test_pin\n",
            3,
            "F1 (tests/test_pin.py::test_pin FAILED)",
            id="mapped-red",
        ),
        pytest.param(None, None, 1, "scope-findings.md missing or unreadable", id="findings-missing"),
    ],
)
def test_team_gate_one_requires_a_passing_test_per_special_case(
    tmp_path: Path, findings: str | None, test_map: str | None, exit_code: int, verdict: str
) -> None:
    """Gate T1 fails while a special case lacks a mapped passing test, or the findings file is missing."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_current.py").write_text("def test_current():\n    assert True\n", encoding="utf-8")
    (tests / "test_pin.py").write_text("def test_pin():\n    assert False\n", encoding="utf-8")
    (tmp_path / "dev-refactor-char-test-refactor-safety").write_text(
        "tests/test_current.py\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("python -m pytest\n", encoding="utf-8", newline="\n")
    _scope_findings(tmp_path, findings or "")
    if findings is None:
        (tmp_path / "scope-findings.md").unlink()
    if test_map is not None:
        (tmp_path / "special-case-tests.md").write_text(test_map, encoding="utf-8")
    result = _run(_block(_TEAM, "Gate T1"), tmp_path)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert verdict in result.stdout


@_skip_no_bash
@pytest.mark.parametrize("gate", ["T1", "T2"])
def test_team_gate_refuses_untrusted_runner(tmp_path: Path, gate: str) -> None:
    """A command planted in runner state cannot execute or become a green gate."""
    target = tmp_path / "tests/test_current.py"
    target.parent.mkdir()
    target.write_text("def test_current():\n    assert True\n", encoding="utf-8")
    (tmp_path / "dev-refactor-char-test-refactor-safety").write_text(
        "tests/test_current.py\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("touch injected\n", encoding="utf-8", newline="\n")
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
def test_missing_quality_stack_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing shipped quality stack is an installation error, never a successful skip."""
    monkeypatch.setenv("HOME", tmp_path.as_posix())
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = tmp_path / ".claude/plugins/cache/borda-ai-rig/develop/1.0.0"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name":"develop"}', encoding="utf-8")
    shared = root / "skills/_shared"
    shared.mkdir(parents=True)
    (tmp_path / "dev-shared-refactor-safety").write_text(shared.as_posix() + "\n", encoding="utf-8", newline="\n")
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
    (tmp_path / "dev-refactor-dev-dir-refactor-safety").write_text(
        tmp_path.as_posix() + "\n", encoding="utf-8", newline="\n"
    )
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
    _scope_findings(tmp_path, plugin=plugin)
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
    (tmp_path / "dev-refactor-char-test-refactor-safety").write_text(path + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("python -m pytest\n", encoding="utf-8", newline="\n")
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
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("python -m pytest\n", encoding="utf-8", newline="\n")
    block = _block(_TEAM, "<char_test_file>").replace("<char_test_file>", path)
    saved = _run(block, tmp_path)
    assert saved.returncode == 0, saved.stderr
    assert (tmp_path / "dev-refactor-char-test-refactor-safety").read_text().strip() == path
    _scope_findings(tmp_path)
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
    assert (shell_path(result.stdout.strip()) == skill) is present
    assert ("! BLOCKED" in result.stdout) is not present


@_skip_no_bash
def test_quality_stack_cold_loads_without_shared_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing warm sentinel cannot veto an available installed quality stack."""
    monkeypatch.setenv("HOME", tmp_path.as_posix())
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    shared = tmp_path / ".claude/plugins/cache/borda-ai-rig/develop/1.0.0/skills/_shared"
    shared.mkdir(parents=True)
    (shared.parents[1] / ".claude-plugin").mkdir()
    (shared.parents[1] / ".claude-plugin/plugin.json").write_text('{"name":"develop"}', encoding="utf-8")
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
    (tmp_path / "dev-refactor-dev-dir-refactor-safety").write_text(
        tmp_path.as_posix() + "\n", encoding="utf-8", newline="\n"
    )
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
    (tmp_path / "dev-refactor-dev-dir-refactor-safety").write_text(
        tmp_path.as_posix() + "\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "test-targets.jsonl").write_text(
        json.dumps({"tests": ["tests/test_existing.py"], "exit": record_exit}) + "\n", encoding="utf-8"
    )
    (scripts / "dev_test_targets.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    _scope_findings(tmp_path, plugin=plugin)
    block = _block(_SKILL, "BASELINE_EXIT").replace("<scoped_python_files>", "pkg/module.py")
    result = _run(block, tmp_path, plugin)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "runner" in result.stdout
    assert "/develop:fix" not in result.stdout


@_skip_no_bash
@pytest.mark.parametrize("marker_newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("existing_passes", "expected_exit", "message"),
    [
        pytest.param(True, 0, "BASELINE OK", id="remaining-tests-green"),
        pytest.param(False, 1, "pre-existing test failure", id="remaining-tests-red"),
    ],
)
def test_accepted_red_characterization_is_excluded_from_baseline_and_t2(
    tmp_path: Path, existing_passes: bool, expected_exit: int, message: str, marker_newline: str
) -> None:
    """Accepted bad assertions are excluded while real source-test failures still block the baseline."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_red.py").write_text("def test_red():\n    assert False\n", encoding="utf-8")
    (tests / "test_existing.py").write_text(f"def test_existing():\n    assert {existing_passes!r}\n", encoding="utf-8")
    (tmp_path / "accepted-red-characterization.txt").write_bytes(f"tests/test_red.py{marker_newline}".encode())
    (tmp_path / "dev-refactor-dev-dir-refactor-safety").write_text(
        tmp_path.as_posix() + "\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "dev-refactor-char-test-refactor-safety").write_text(
        "tests/test_red.py\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("python -m pytest\n", encoding="utf-8", newline="\n")
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
    _scope_findings(tmp_path, plugin=plugin)
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


_SPECIAL = "F1 · special-case · pkg/mod.py::retry · retries once\n"
_TWO_SPECIAL = _SPECIAL + "F2 · special-case · pkg/mod.py::other · one caller\n"
_PIN_TESTS = (
    "import pytest\n"
    "def test_pass():\n    assert True\n"
    "def test_pass_too():\n    assert True\n"
    "def test_fail():\n    assert False\n"
    "@pytest.mark.skip(reason='x')\ndef test_skip():\n    pass\n"
    "@pytest.mark.skipif(True, reason='x')\ndef test_skipif_true():\n    pass\n"
    "@pytest.mark.skipif(False, reason='x')\ndef test_skipif_false():\n    pass\n"
    "@pytest.mark.xfail\ndef test_xfail():\n    assert False\n"
    "@pytest.mark.xfail\ndef test_xpass():\n    assert True\n"
    "@pytest.mark.xfail(strict=False)\ndef test_xpass_nonstrict():\n    assert True\n"
    "def test_imperative_xfail():\n    pytest.xfail('runtime')\n"
    "@pytest.mark.parametrize('value', ['a b', '[x]', 'é'])\n"
    "def test_param(value):\n    if value == 'a b':\n        pytest.skip('space id')\n"
    "@pytest.mark.parametrize('value', ['a b', '[x]', 'é'])\n"
    "def test_param_ok(value):\n    assert value\n"
    "def test_runtime_xpass(request):\n    request.applymarker(pytest.mark.xfail(strict=False))\n"
    "@pytest.fixture\ndef broken_teardown():\n    yield\n    raise RuntimeError('teardown')\n"
    "def test_teardown_error(broken_teardown):\n    assert True\n"
)


def _pin_fixture(tmp_path: Path, findings: str, test_map: str | None, char_body: str = "") -> None:
    """Lay out a characterization file, pin tests in another file, findings and map, plus runner state."""
    tests = tmp_path / "tests"
    tests.mkdir()
    body = char_body or "def test_current():\n    assert True\n"
    (tests / "test_current.py").write_text(body, encoding="utf-8")
    (tests / "test_pin.py").write_text(_PIN_TESTS, encoding="utf-8")
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("python -m pytest\n", encoding="utf-8", newline="\n")
    (tmp_path / "dev-refactor-char-test-refactor-safety").write_text(
        "tests/test_current.py\n", encoding="utf-8", newline="\n"
    )
    _scope_findings(tmp_path, findings)
    if test_map is not None:
        (tmp_path / "special-case-tests.md").write_text(test_map, encoding="utf-8")


def _solo_gate(tmp_path: Path, plugin: Path = _PLUGIN) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the solo Step 3 gate against ``tests/test_current.py``; return the result and the recorded gate exit."""
    result = _run(_block(_SKILL, "<test_file>").replace("<test_file>", "tests/test_current.py"), tmp_path, plugin)
    return result, (tmp_path / "dev-refactor-gate-exit-refactor-safety").read_text().strip()


def _accept(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the solo Step 3 gate for a current verdict, then the shipped accepted-risk block."""
    _solo_gate(tmp_path)
    return _run(_block(_SKILL, "accept-unpinned"), tmp_path)


@_skip_no_bash
@pytest.mark.parametrize(
    ("test_map", "exit_code", "line", "cause"),
    [
        pytest.param(None, 3, "GATE FAIL", "F1 (unmapped)", id="unmapped"),
        pytest.param("test_pass", 0, "GATE OK", "SPECIAL-CASE PIN OK", id="pass"),
        pytest.param("./tests/test_pin.py::test_pass", 0, "GATE OK", "SPECIAL-CASE PIN OK", id="dot-slash-path"),
        pytest.param("test_fail", 3, "GATE FAIL", "F1 (tests/test_pin.py::test_fail FAILED)", id="fail"),
        pytest.param("test_skip", 3, "GATE FAIL", "F1 (tests/test_pin.py::test_skip SKIPPED", id="skip"),
        pytest.param(
            "test_skipif_true", 3, "GATE FAIL", "F1 (tests/test_pin.py::test_skipif_true SKIPPED", id="skipif-true"
        ),
        pytest.param("test_skipif_false", 0, "GATE OK", "SPECIAL-CASE PIN OK", id="skipif-false-ran"),
        pytest.param("test_xfail", 3, "GATE FAIL", "F1 (tests/test_pin.py::test_xfail XFAIL", id="xfail"),
        pytest.param("test_xpass", 3, "GATE FAIL", "F1 (tests/test_pin.py::test_xpass XPASS", id="xpass-strict-by-ini"),
        pytest.param(
            "test_xpass_nonstrict",
            3,
            "GATE FAIL",
            "F1 (tests/test_pin.py::test_xpass_nonstrict XPASS)",
            id="xpass-nonstrict",
        ),
        pytest.param(
            "test_imperative_xfail",
            3,
            "GATE FAIL",
            "F1 (tests/test_pin.py::test_imperative_xfail XFAIL",
            id="imperative-xfail",
        ),
        pytest.param(
            "test_param", 3, "GATE FAIL", "F1 (tests/test_pin.py::test_param SKIPPED", id="param-id-with-space-skips"
        ),
        pytest.param("test_param_ok", 0, "GATE OK", "SPECIAL-CASE PIN OK", id="param-ids-space-brackets-unicode"),
        pytest.param("test_absent", 6, "! GATE ERROR", "pytest usage error", id="usage-error-no-verdict"),
        pytest.param(
            "test_runtime_xpass", 3, "GATE FAIL", "F1 (tests/test_pin.py::test_runtime_xpass XPASS)", id="runtime-xpass"
        ),
        pytest.param(
            "test_teardown_error",
            3,
            "GATE FAIL",
            "F1 (tests/test_pin.py::test_teardown_error ERROR)",
            id="fixture-error",
        ),
    ],
)
def test_solo_characterization_gate_pins_every_special_case(
    tmp_path: Path, test_map: str | None, exit_code: int, line: str, cause: str
) -> None:
    """The solo Step 3 gate pins a special case only through a real pass, and names every other outcome."""
    row = None if test_map is None else f"F1 · {test_map if '::' in test_map else 'tests/test_pin.py::' + test_map}\n"
    _pin_fixture(tmp_path, _SPECIAL, row)
    result, recorded = _solo_gate(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert recorded == str(exit_code)
    assert line in result.stdout
    assert cause in result.stdout


@_skip_no_bash
def test_solo_characterization_gate_names_foreign_rootdir(tmp_path: Path) -> None:
    """Report ids relative to another pytest rootdir are a no-verdict error, never a failing special case."""
    _pin_fixture(tmp_path, _SPECIAL, "F1 · tests/test_pin.py::test_pass\n")
    (tmp_path / "tests/pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    result, recorded = _solo_gate(tmp_path)
    assert recorded == "6", result.stdout + result.stderr
    assert "rootdir differs" in result.stdout
    assert "! GATE ERROR" in result.stdout
    assert "GATE FAIL" not in result.stdout


@_skip_no_bash
def test_solo_characterization_gate_blocks_invalid_map(tmp_path: Path) -> None:
    """A map row naming a missing test file is a blocked map, not a failing special case."""
    _pin_fixture(tmp_path, _SPECIAL, "F1 · tests/test_absent.py::test_x\n")
    result, recorded = _solo_gate(tmp_path)
    assert recorded == "1", result.stdout + result.stderr
    assert "mapped test file missing for F1" in result.stdout
    assert "GATE FAIL" not in result.stdout


@_skip_no_bash
@pytest.mark.parametrize(
    ("char_body", "recorded", "verdict"),
    [
        pytest.param("def test_current():\n    assert False\n", "1", "characterization test(s) failed", id="red"),
        pytest.param("value = 1\n", "5", "no tests collected", id="no-tests"),
    ],
)
def test_solo_characterization_gate_names_characterization_failures(
    tmp_path: Path, char_body: str, recorded: str, verdict: str
) -> None:
    """A red or empty characterization run is reported as such; the pin still runs so (b) can show its labels."""
    _pin_fixture(tmp_path, _SPECIAL, "F1 · tests/test_pin.py::test_pass\n", char_body)
    result, gate_exit = _solo_gate(tmp_path)
    assert gate_exit == recorded, result.stdout + result.stderr
    assert verdict in result.stdout
    assert "SPECIAL-CASE PIN OK: 1 special case(s) pinned" in result.stdout
    assert "GATE OK" not in result.stdout


@_skip_no_bash
def test_solo_characterization_gate_names_runner_refusal(tmp_path: Path) -> None:
    """A refused runner is reported as a runner error, never as an unpinned special case."""
    _pin_fixture(tmp_path, _SPECIAL, "F1 · tests/test_pin.py::test_pass\n")
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("touch injected\n", encoding="utf-8", newline="\n")
    result, recorded = _solo_gate(tmp_path)
    assert recorded == "64"
    assert "GATE ERROR" in result.stdout
    assert "not pinned" not in result.stdout


def _faulting_runner_plugin(tmp_path: Path, when: str) -> Path:
    """Return a fake plugin whose test runner exits 70 for the characterization run or for the pin run only."""
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    shutil.copy(_PLUGIN / "bin/dev_scope_findings.py", plugin / "bin/dev_scope_findings.py")
    fault = "'--junitxml' in ' '.join(sys.argv)" if when == "pin" else "'--junitxml' not in ' '.join(sys.argv)"
    (plugin / "bin/dev_run_tests.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(_PLUGIN / 'bin')!r})\n"
        f"if {fault}:\n"
        "    raise SystemExit(70)\n"
        "from dev_run_tests import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    return plugin


@_skip_no_bash
@pytest.mark.parametrize(
    ("when", "verdict"),
    [
        pytest.param("characterization", "test runner fault (exit 70)", id="characterization-run"),
        pytest.param("pin", "test runner did not run the special-case tests (exit 70)", id="pin-run"),
    ],
)
def test_solo_characterization_gate_names_runner_fault(tmp_path: Path, when: str, verdict: str) -> None:
    """A runner fault in either run keeps its own exit code and message."""
    _pin_fixture(tmp_path, _SPECIAL, "F1 · tests/test_pin.py::test_pass\n")
    result, recorded = _solo_gate(tmp_path, _faulting_runner_plugin(tmp_path, when))
    assert recorded == "70", result.stdout + result.stderr
    assert verdict in result.stdout
    assert "GATE FAIL" not in result.stdout


@_skip_no_bash
@pytest.mark.parametrize(
    ("test_map", "exit_code", "verdict"),
    [
        pytest.param("F1 · tests/test_pin.py::test_pass\n", 0, "GATE OK", id="pinned"),
        pytest.param("F1 · tests/test_pin.py::test_absent\n", 6, "pytest usage error", id="usage-error"),
    ],
)
def test_team_gate_one_names_pin_cause(tmp_path: Path, test_map: str, exit_code: int, verdict: str) -> None:
    """Gate T1 keeps a no-verdict special-case run distinct from an unpinned special case."""
    _pin_fixture(tmp_path, _SPECIAL, test_map)
    result = _run(_block(_TEAM, "Gate T1"), tmp_path)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert verdict in result.stdout


@_skip_no_bash
def test_team_gate_one_names_characterization_runner_fault(tmp_path: Path) -> None:
    """A runner fault on the characterization run is a GATE ERROR, not a failed characterization."""
    _pin_fixture(tmp_path, _SPECIAL, "F1 · tests/test_pin.py::test_pass\n")
    result = _run(_block(_TEAM, "Gate T1"), tmp_path, _faulting_runner_plugin(tmp_path, "characterization"))
    assert result.returncode == 70, result.stdout + result.stderr
    assert "! GATE ERROR: test runner fault (exit 70)" in result.stdout


class TestAcceptedRisk:
    """Accepting a GATE FAIL waives exactly the unpinned labels, only for the content it was recorded against."""

    @_skip_no_bash
    def test_accepts_only_unpinned_labels(self, tmp_path: Path) -> None:
        """A failing label is accepted; a passing label stays enforced and keeps running."""
        _pin_fixture(tmp_path, _TWO_SPECIAL, "F1 · tests/test_pin.py::test_pass\nF2 · tests/test_pin.py::test_fail\n")
        accept = _accept(tmp_path)
        assert accept.returncode == 0, accept.stdout + accept.stderr
        assert "ACCEPTED UNPINNED: F2" in accept.stdout
        pin = _run(_block(_TEAM, "Gate T2"), tmp_path)
        assert pin.returncode == 0, pin.stdout + pin.stderr
        assert "SPECIAL-CASE PIN OK: 1 special case(s) pinned" in pin.stdout

    @_skip_no_bash
    def test_stale_acceptance_is_ignored_after_the_map_changes(self, tmp_path: Path) -> None:
        """Repointing an accepted label at a failing test after acceptance re-enforces it instead of passing."""
        _pin_fixture(tmp_path, _TWO_SPECIAL, "F1 · tests/test_pin.py::test_pass\n")
        accept = _accept(tmp_path)
        assert "ACCEPTED UNPINNED: F2" in accept.stdout, accept.stdout + accept.stderr
        (tmp_path / "special-case-tests.md").write_text(
            "F1 · tests/test_pin.py::test_pass\nF2 · tests/test_pin.py::test_fail\n", encoding="utf-8"
        )
        pin = _run(_block(_TEAM, "Gate T2"), tmp_path)
        assert pin.returncode == 3, pin.stdout + pin.stderr
        assert "STALE ACCEPTANCE" in pin.stdout
        assert "F2 (tests/test_pin.py::test_fail FAILED)" in pin.stdout
        assert "PIN OK" not in pin.stdout

    @_skip_no_bash
    @pytest.mark.parametrize(
        ("test_map", "plugin_fault", "gate_cause", "exit_code", "cause"),
        [
            pytest.param(
                "F1 · tests/test_absent.py::test_x\n",
                False,
                "mapped test file missing for F1",
                1,
                "mapped test file missing for F1",
                id="invalid-map",
            ),
            pytest.param(
                "F1 · tests/test_pin.py::test_absent\n",
                False,
                "pytest usage error",
                6,
                "no current Step 3 verdict",
                id="no-verdict",
            ),
            pytest.param(
                "F1 · tests/test_pin.py::test_pass\n",
                True,
                "test runner did not run the special-case tests (exit 70)",
                6,
                "no current Step 3 verdict",
                id="runner-fault",
            ),
        ],
    )
    def test_no_verdict_accepts_nothing(
        self, tmp_path: Path, test_map: str, plugin_fault: bool, gate_cause: str, exit_code: int, cause: str
    ) -> None:
        """After an invalid map, a run without verdict or a runner fault, the accept block records nothing."""
        _pin_fixture(tmp_path, _SPECIAL, test_map)
        plugin = _faulting_runner_plugin(tmp_path, "pin") if plugin_fault else _PLUGIN
        gate, _ = _solo_gate(tmp_path, plugin)
        assert gate_cause in gate.stdout, gate.stdout + gate.stderr
        accept = _run(_block(_SKILL, "accept-unpinned"), tmp_path, plugin)
        assert accept.returncode == exit_code, accept.stdout + accept.stderr
        assert cause in accept.stdout
        assert not (tmp_path / "accepted-unpinned.json").exists()

    @_skip_no_bash
    def test_red_characterization_still_shows_and_binds_the_unpinned_labels(self, tmp_path: Path) -> None:
        """A red characterization run still prints the pin's labels, and acceptance records exactly those."""
        _pin_fixture(
            tmp_path,
            _TWO_SPECIAL,
            "F1 · tests/test_pin.py::test_pass\nF2 · tests/test_pin.py::test_fail\n",
            "def test_current():\n    assert False\n",
        )
        gate, recorded = _solo_gate(tmp_path)
        assert recorded == "1", gate.stdout + gate.stderr
        assert "GATE FAIL: special case not pinned — F2 (tests/test_pin.py::test_fail FAILED)" in gate.stdout
        assert "characterization test(s) failed" in gate.stdout
        (tmp_path / "tests/test_pin.py").write_text(
            _PIN_TESTS.replace("def test_pass():\n    assert True", "def test_pass():\n    assert False"),
            encoding="utf-8",
        )
        accept = _run(_block(_SKILL, "accept-unpinned"), tmp_path)
        assert accept.returncode == 0, accept.stdout + accept.stderr
        assert "ACCEPTED UNPINNED: F2" in accept.stdout
        assert "F1" not in accept.stdout.split("ACCEPTED UNPINNED:")[1]

    @_skip_no_bash
    def test_reset_clears_map_and_acceptance_while_checks_keep_them(self, tmp_path: Path) -> None:
        """Only the reset run at Step 1 and first Step 3 entry clears state; later check and literal blocks keep it."""
        _pin_fixture(tmp_path, _TWO_SPECIAL, "F1 · tests/test_pin.py::test_pass\nF2 · tests/test_pin.py::test_fail\n")
        assert _accept(tmp_path).returncode == 0
        for kept in (_block(_TEAM, 'check "$DEV_DIR" || exit 1'), _block(_SKILL, 'check "$DEV_DIR"  # timeout')):
            assert _run(kept, tmp_path).returncode == 0
        assert _run(_block(_TEAM, "Gate T2"), tmp_path).returncode == 0
        assert (tmp_path / "accepted-unpinned.json").exists()
        assert (tmp_path / "special-case-tests.md").exists()
        reset = _run(_block(_SKILL, "premise-grounding.md"), tmp_path)
        assert reset.returncode == 0, reset.stdout + reset.stderr
        assert not (tmp_path / "accepted-unpinned.json").exists()
        assert not (tmp_path / "special-case-tests.md").exists()


def _baseline_plugin(tmp_path: Path, selection: list[str]) -> Path:
    """Return a fake plugin whose selector proposes ``selection`` and runs the real selector, plus the real helper."""
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    (plugin / "bin/dev_test_targets.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(_PLUGIN / 'bin')!r})\n"
        "import dev_test_targets as selector\n"
        "if '--files' in sys.argv and '--run' not in sys.argv:\n"
        f"    print(json.dumps({{'tests': {selection!r}}}))\n"
        "    raise SystemExit(0)\n"
        "if '--files' in sys.argv:\n"
        "    rest = sys.argv[sys.argv.index('--record-dir'):]\n"
        "    sys.argv = [sys.argv[0], '--skill', 'refactor', '--tests-from', 'sel.json', *rest]\n"
        f"    Path('sel.json').write_text(json.dumps({selection!r}))\n"
        "selector._repo_root = Path.cwd\n"
        "raise SystemExit(selector.main())\n",
        encoding="utf-8",
    )
    shutil.copy(_PLUGIN / "bin/dev_scope_findings.py", plugin / "bin/dev_scope_findings.py")
    (plugin / "bin/dev_run_tests.py").write_text(
        f"import sys\nsys.path.insert(0, {str(_PLUGIN / 'bin')!r})\nfrom dev_run_tests import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    return plugin


def _baseline(tmp_path: Path, plugin: Path, files: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the shipped pre-edit baseline over ``files``."""
    block = _block(_SKILL, "BASELINE_EXIT").replace("'<scoped_python_files>'", shlex.join(files))
    return _run(block, tmp_path, plugin)


@_skip_no_bash
@pytest.mark.parametrize("accepted", [True, False], ids=["stale-acceptance", "pin-ok-then-map-edit"])
def test_step_four_entry_blocks_a_map_edited_after_the_gate(tmp_path: Path, accepted: bool) -> None:
    """Repointing the map at a failing test after Step 3 blocks Step 4 until the Step 3 gate runs again."""
    if accepted:
        _pin_fixture(tmp_path, _TWO_SPECIAL, "F1 · tests/test_pin.py::test_pass\n")
        assert "ACCEPTED UNPINNED: F2" in _accept(tmp_path).stdout
        rows = "F1 · tests/test_pin.py::test_pass\nF2 · tests/test_pin.py::test_fail\n"
    else:
        _pin_fixture(tmp_path, _SPECIAL, "F1 · tests/test_pin.py::test_pass\n")
        gate, recorded = _solo_gate(tmp_path)
        assert recorded == "0", gate.stdout + gate.stderr
        rows = "F1 · tests/test_pin.py::test_fail\n"
    (tmp_path / "special-case-tests.md").write_text(rows, encoding="utf-8")
    plugin = _baseline_plugin(tmp_path, ["tests/test_current.py"])
    result = _baseline(tmp_path, plugin, ["tests/test_current.py"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "no current Step 3 verdict for these findings and map — re-run the Step 3 gate" in result.stdout
    assert "BASELINE OK" not in result.stdout


@_skip_no_bash
def test_baseline_keeps_a_node_shared_with_a_pinned_label(tmp_path: Path) -> None:
    """Accepting a label leaves out only its failing node, never a node a pinned label also maps."""
    _pin_fixture(
        tmp_path,
        _TWO_SPECIAL,
        "F1 · tests/test_pin.py::test_fail\nF1 · tests/test_pin.py::test_shared\nF2 · tests/test_pin.py::test_shared\n",
    )
    (tmp_path / "tests/test_pin.py").write_text(
        "def test_fail():\n    assert False\n\ndef test_shared():\n    assert True\n", encoding="utf-8"
    )
    assert "ACCEPTED UNPINNED: F1" in _accept(tmp_path).stdout
    plugin = _baseline_plugin(tmp_path, ["tests/test_pin.py"])
    result = _baseline(tmp_path, plugin, ["tests/test_pin.py"])
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((tmp_path / "test-targets.jsonl").read_text().splitlines()[-1])
    assert record["tests"] == ["tests/test_pin.py::test_shared"]


@_skip_no_bash
@pytest.mark.parametrize(
    ("layout", "ini", "node"),
    [
        pytest.param("tests/Unit/test_u.py", "", "tests/Unit/test_u.py::test_u", id="uppercase-directory"),
        pytest.param(
            "tests/test_k.py",
            "[pytest]\npython_classes = check_*\n",
            "tests/test_k.py::check_k::test_u",
            id="lowercase-class",
        ),
    ],
)
def test_report_ids_map_back_through_the_file_attribute(tmp_path: Path, layout: str, ini: str, node: str) -> None:
    """Uppercase directories and lowercase classes keep their own outcome instead of reading as not reported."""
    _pin_fixture(tmp_path, _SPECIAL, f"F1 · {node}\n")
    target = tmp_path / layout
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "class check_k:\n    def test_u(self):\n        assert True\n" if ini else "def test_u():\n    assert True\n"
    target.write_text(body, encoding="utf-8")
    if ini:
        (tmp_path / "pytest.ini").write_text(ini, encoding="utf-8")
    gate, recorded = _solo_gate(tmp_path)
    assert recorded == "0", gate.stdout + gate.stderr
    assert "SPECIAL-CASE PIN OK: 1 special case(s) pinned" in gate.stdout


@_skip_no_bash
@pytest.mark.parametrize("accepted", [False, True], ids=["unmapped-blocks", "accepted-risk-passes"])
def test_baseline_map_blocks_unmapped_special_cases_until_accepted(tmp_path: Path, accepted: bool) -> None:
    """An unmapped special case stops source edits; the accepted-risk block opens a route to Step 4."""
    _pin_fixture(tmp_path, _SPECIAL, None)
    plugin = _baseline_plugin(tmp_path, ["tests/test_current.py"])
    if accepted:
        accept = _accept(tmp_path)
        assert accept.returncode == 0, accept.stdout + accept.stderr
        assert "ACCEPTED UNPINNED: F1" in accept.stdout
    else:
        gate, _ = _solo_gate(tmp_path)
        assert "F1 (unmapped)" in gate.stdout, gate.stdout + gate.stderr
    result = _baseline(tmp_path, plugin, ["tests/test_current.py"])
    assert result.returncode == (0 if accepted else 1), result.stdout + result.stderr
    assert ("BASELINE OK" in result.stdout) is accepted
    assert ("special case(s) F1 not pinned and not accepted" in result.stdout) is not accepted


@_skip_no_bash
@pytest.mark.parametrize("accepted", [False, True], ids=["mapped-red-blocks", "accepted-risk-excludes-node"])
def test_baseline_excludes_only_accepted_failing_nodes(tmp_path: Path, accepted: bool) -> None:
    """An unaccepted failing pin blocks Step 4; once accepted only its node leaves the baseline, its file still runs."""
    _pin_fixture(tmp_path, _TWO_SPECIAL, "F1 · tests/test_pin.py::test_fail\nF2 · tests/test_pin.py::test_pass\n")
    (tmp_path / "tests/test_pin.py").write_text(
        "def test_pass():\n    assert True\n\ndef test_fail():\n    assert False\n\ndef test_other():\n    assert True\n",
        encoding="utf-8",
    )
    plugin = _baseline_plugin(tmp_path, ["tests/test_pin.py", "tests/test_current.py"])
    if accepted:
        accept = _accept(tmp_path)
        assert accept.returncode == 0, accept.stdout + accept.stderr
        assert "ACCEPTED UNPINNED: F1" in accept.stdout
    else:
        gate, _ = _solo_gate(tmp_path)
        assert "F1 (tests/test_pin.py::test_fail FAILED)" in gate.stdout, gate.stdout + gate.stderr
    result = _baseline(tmp_path, plugin, ["tests/test_pin.py", "tests/test_current.py"])
    assert result.returncode == (0 if accepted else 1), result.stdout + result.stderr
    assert ("BASELINE OK" in result.stdout) is accepted
    assert ("special case(s) F1 not pinned and not accepted" in result.stdout) is not accepted
    if accepted:
        record = json.loads((tmp_path / "test-targets.jsonl").read_text().splitlines()[-1])
        assert record["tests"] == [
            "tests/test_pin.py::test_pass",
            "tests/test_pin.py::test_other",
            "tests/test_current.py",
        ]


@_skip_no_bash
@pytest.mark.parametrize(
    ("test_map", "accept_red_char", "accepted_f1", "exit_code", "verdict"),
    [
        pytest.param("F1 · tests/test_pin.py::test_pass\n", False, False, 0, "SPECIAL-CASE PIN OK", id="pinned"),
        pytest.param(
            "F1 · tests/test_pin.py::test_fail\n", False, False, 3, "not passing after refactor", id="mapped-red"
        ),
        pytest.param(
            "F1 · tests/test_pin.py::test_fail\nF2 · tests/test_pin.py::test_pass\n",
            False,
            True,
            0,
            "SPECIAL-CASE PIN OK: 1 special case(s) pinned",
            id="accepted-unpinned-left-out",
        ),
        pytest.param(
            "F1 · tests/test_pin.py::test_pass\nF2 · tests/test_pin.py::test_fail\n",
            True,
            False,
            3,
            "not passing after refactor",
            id="accepted-red-char-still-runs-pins",
        ),
        pytest.param("F1 · tests/test_pin.py::test_absent\n", False, False, 6, "no verdict", id="pin-no-verdict"),
        pytest.param("F1 · tests/test_pin.py::test_fail\n", False, False, 3, "GATE FAIL", id="single-verdict-line"),
    ],
)
def test_team_gate_two_reruns_every_mapped_test(
    tmp_path: Path, test_map: str, accept_red_char: bool, accepted_f1: bool, exit_code: int, verdict: str
) -> None:
    """Gate T2 reruns mapped tests from any file, leaves out only accepted labels, and names a no-verdict run."""
    _pin_fixture(tmp_path, _TWO_SPECIAL if "F2" in test_map else _SPECIAL, test_map)
    if accept_red_char:
        (tmp_path / "accepted-red-characterization.txt").write_text("tests/test_current.py\n", encoding="utf-8")
    if accepted_f1:
        assert "ACCEPTED UNPINNED: F1" in _accept(tmp_path).stdout
    result = _run(_block(_TEAM, "Gate T2"), tmp_path)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert verdict in result.stdout
    assert ("accepted-red characterization test excluded" in result.stdout) is accept_red_char
    assert ("GATE OK" in result.stdout) is (exit_code == 0 and not accept_red_char)


@_skip_no_bash
def test_team_gate_two_runs_pins_after_accepted_empty_characterization(tmp_path: Path) -> None:
    """An accepted exit-5 characterization file is recorded like a red one, so T2 still reaches the pins."""
    _pin_fixture(tmp_path, _SPECIAL, "F1 · tests/test_pin.py::test_pass\n", "value = 1\n")
    (tmp_path / "accepted-red-characterization.txt").write_text("tests/test_current.py\n", encoding="utf-8")
    result = _run(_block(_TEAM, "Gate T2"), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "accepted-red characterization test excluded" in result.stdout
    assert "SPECIAL-CASE PIN OK: 1 special case(s) pinned" in result.stdout


@_skip_no_bash
@pytest.mark.parametrize("present", [True, False], ids=["findings-present", "findings-missing"])
def test_boundary_two_checks_the_findings_file(tmp_path: Path, present: bool) -> None:
    """The post-edit compaction boundary refuses to continue without a valid findings file."""
    _scope_findings(tmp_path)
    if not present:
        (tmp_path / "scope-findings.md").unlink()
    (tmp_path / "dev-pytest-cmd-refactor-safety").write_text("python -m pytest\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    result = _run(_block(_SKILL, "review+quality"), tmp_path)
    assert (result.returncode == 0) is present, result.stdout + result.stderr
    assert ("! BLOCKED" in result.stdout) is not present
    assert ("missing or unreadable" in result.stdout) is not present


@_skip_no_bash
def test_team_gate_two_names_characterization_runner_fault(tmp_path: Path) -> None:
    """A runner fault on the T2 characterization run is a GATE ERROR with its own exit code."""
    _pin_fixture(tmp_path, _SPECIAL, "F1 · tests/test_pin.py::test_pass\n")
    result = _run(_block(_TEAM, "Gate T2"), tmp_path, _faulting_runner_plugin(tmp_path, "characterization"))
    assert result.returncode == 70, result.stdout + result.stderr
    assert "! GATE ERROR: test runner fault (exit 70)" in result.stdout
    assert "GATE OK" not in result.stdout
