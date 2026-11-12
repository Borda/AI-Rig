"""Execute skill shell blocks to check scan state, failure exits, and index build opt-outs."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import test_impact_union
from _codemap_shell_plugin import render_shell
from _host_skips import needs_git_and_bash
from _scan_helpers import index_path_for, run_query, run_scan

PLUGIN = Path(__file__).resolve().parents[2]
_SETUP_BOUNDARY = """
python() {
    case "$1" in
        */setup_scan_env.py) printf '%s\\n' "$STATE_FILE";;
        *) "$PYTHON" "$@";;
    esac
}
"""


def _bash_blocks(skill: str) -> list[str]:
    """Read Bash fences line by line without depending on prose wrapping."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in (PLUGIN / "claude-skills" / skill / "SKILL.md").read_text(encoding="utf-8").splitlines():
        if line == "```bash":
            current = []
        elif line == "```" and current is not None:
            blocks.append("\n".join(current))
            current = None
        elif current is not None:
            current.append(line)
    return blocks


@pytest.fixture(name="shell_env")
def _shell_env(tmp_path: Path) -> dict[str, str]:
    """Isolate sentinels and run real Python helpers from the active test interpreter."""
    return {
        **os.environ,
        "TMPDIR": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
        "CLAUDE_CODE_SESSION_ID": "skill-safety",
        "PYTHON": sys.executable,
        "ARGUMENTS": "",
        "CODEMAP_INDEX_DIR": str(tmp_path / "index"),
    }


def _shell(block: str, tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a shipped block with helper and dispatcher boundaries exposed for assertions."""
    prelude = """
python() { "$PYTHON" "$@"; }
codemap-py() { printf '%s\\n' "$@" > "$TMPDIR/dispatcher-args"; }
git() { return 1; }
"""
    block = block.replace(
        'SCAN_STATE_FILE="<state-file-path-from-Step-1>"', f"SCAN_STATE_FILE={shlex.quote(env.get('STATE_FILE', ''))}"
    )
    return subprocess.run(
        ["bash", "-c", render_shell(prelude + block, PLUGIN, "codemap-py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@needs_git_and_bash
@pytest.mark.parametrize("opt_out", ["0", "1"])
def test_missing_index_build_obeys_opt_out(tmp_path: Path, shell_env: dict[str, str], opt_out: str) -> None:
    """The missing-index block must refuse an opted-out build before calling the dispatcher."""
    block = _bash_blocks("test-impact")[1]
    result = _shell(block, tmp_path, {**shell_env, "SCAN_NO_AUTOBUILD": opt_out})

    assert result.returncode == (1 if opt_out == "1" else 0), result.stdout + result.stderr
    assert (tmp_path / "dispatcher-args").exists() == (opt_out == "0")


def _scan_state(tmp_path: Path, raw_args: str = "", project_name: str = "project") -> Path:
    """Build state through the installed writer and argument formatter, with unknown text kept as data."""
    setup_spec = importlib.util.spec_from_file_location("scan_setup_fixture", PLUGIN / "bin" / "setup_scan_env.py")
    setup = importlib.util.module_from_spec(setup_spec)
    setup_spec.loader.exec_module(setup)
    args_spec = importlib.util.spec_from_file_location("scan_args_fixture", PLUGIN / "bin" / "parse_scan_args.py")
    args = importlib.util.module_from_spec(args_spec)
    args_spec.loader.exec_module(args)
    values = {
        "PROJ_SLUG": "scan-project",
        "SCAN_BIN": "unused",
        "SCAN_ARGS_RAW": args.format_scan_args(shlex.split(raw_args)),
        "PROJ_NAME": project_name,
    }
    directory = tmp_path / "private-scan-state"
    directory.mkdir(mode=0o700, exist_ok=True)
    state = setup._write_state_file(directory, values)
    with state.open("a", encoding="utf-8") as stream:
        stream.write("UNKNOWN='$(printf exploited > \"$TMPDIR/executed\")'\n")
    return state


@needs_git_and_bash
def test_scan_step_one_reads_state_without_execution(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """State text cannot execute, and spaces, equals signs and shell metacharacters survive as arguments."""
    root = "source = $literal `name`"
    state = _scan_state(tmp_path, f'--root "{root}"')
    result = _shell(
        _SETUP_BOUNDARY + _bash_blocks("scan-codebase")[0], tmp_path, {**shell_env, "STATE_FILE": str(state)}
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "executed").exists()
    assert (tmp_path / "dispatcher-args").read_text(encoding="utf-8").splitlines() == [
        "index",
        "--timeout",
        "360",
        "--root",
        root,
    ]


@pytest.mark.parametrize("surface", ["$ARGUMENTS", "AskUserQuestion"])
def test_test_impact_stops_unsafe_text_before_substitution(surface: str) -> None:
    """Every text interpolation surface must have an explicit pre-execution stop instruction."""
    lines = (PLUGIN / "claude-skills" / "test-impact" / "SKILL.md").read_text(encoding="utf-8").splitlines()
    guard = next((line for line in lines if "do not run" in line and surface in line), "")

    assert all(token in guard for token in ("double quote", "backtick", "backslash", "$", "stop"))
    assert lines.index(guard) < next(i for i, line in enumerate(lines) if line == "```bash")


def test_omitted_target_covers_whole_change_before_asking_for_one_module() -> None:
    """Whole-change requests must route to a union and never silently choose one changed module."""
    lines = (PLUGIN / "claude-skills" / "test-impact" / "SKILL.md").read_text(encoding="utf-8").splitlines()
    routing = next((line for line in lines if "more than one" in line and "diff-impact" in line), "")
    scope = next((line for line in lines if "other changed modules not analysed" in line), "")

    assert "test_impact" in routing
    assert "Step 3" in routing
    assert "len(changed_modules) + len(unmapped_files) > 1" in routing
    assert "<qname> only" in scope
    assert any("--no-mocks" in line and "each changed module" in line and "changed_symbols" in line for line in lines)
    assert any("**When `total == 0`**" in line and "Step 1 whole-change" in line for line in lines)


@needs_git_and_bash
@pytest.mark.parametrize("step", [0, 1])
@pytest.mark.parametrize("root", ["source\nsecond", "source\nsecond'"])
def test_multiline_scan_state_is_rejected_before_use(
    tmp_path: Path, shell_env: dict[str, str], step: int, root: str
) -> None:
    """A writer-emitted multiline value must fail instead of scanning or reporting a truncated root."""
    state = _scan_state(tmp_path, f'--root "{root}"')
    _report_state(tmp_path, state)

    result = _shell(
        _SETUP_BOUNDARY + _bash_blocks("scan-codebase")[step], tmp_path, {**shell_env, "STATE_FILE": str(state)}
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "state incomplete" in result.stdout + result.stderr
    assert not (tmp_path / "dispatcher-args").exists()


@needs_git_and_bash
@pytest.mark.parametrize("query_output", ["failure", "not-json"])
def test_whole_change_failure_reaches_question_route(
    tmp_path: Path, shell_env: dict[str, str], query_output: str
) -> None:
    """A failed or unparsable diff query reports its diagnostic without abandoning target selection."""
    (tmp_path / "diff-stdout").write_text(query_output, encoding="utf-8")
    block = next(block for block in _bash_blocks("test-impact") if "query diff-impact" in block)
    prelude = """
codemap-py() {
    cat "$TMPDIR/diff-stdout"
    printf 'git diff diagnostic\\n' >&2
    [ "$(cat "$TMPDIR/diff-stdout")" = not-json ]
}
"""

    result = _shell(prelude + block, tmp_path, shell_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "whole-change analysis unavailable" in result.stdout + result.stderr
    assert "git diff diagnostic" in result.stdout + result.stderr
    assert "# timeout: 120000" in block


def test_whole_change_mock_exclusion_retains_same_module_test_callers(
    tmp_path: Path,
    scan_index: Path,
    scan_query: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Changed-symbol queries retain local test callers that module import traversal cannot discover."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_local.py").write_text(
        "def helper():\n    return 2\n\ndef test_helper():\n    assert helper() == 2\n", encoding="utf-8"
    )
    (tmp_path / "other.py").write_text("def other():\n    return 2\n", encoding="utf-8")
    diff = tmp_path / "change.diff"
    diff.write_text(
        "--- a/tests/test_local.py\n+++ b/tests/test_local.py\n@@ -1,2 +1,2 @@\n"
        " def helper():\n-    return 1\n+    return 2\n"
        "--- a/other.py\n+++ b/other.py\n@@ -1,2 +1,2 @@\n def other():\n-    return 1\n+    return 2\n",
        encoding="utf-8",
    )
    run_scan(scan_index, tmp_path)
    index = index_path_for(tmp_path)

    change = run_query(scan_query, tmp_path, index, "--no-heal", "diff-impact", "--diff-file", str(diff))
    module = run_query(scan_query, tmp_path, index, "--no-heal", "test-impact", "tests.test_local", "--no-mocks")
    symbol = run_query(
        scan_query, tmp_path, index, "--no-heal", "test-impact", "tests.test_local::helper", "--no-mocks"
    )

    assert len(change["changed_modules"]) == 2
    assert change["test_impact"]["test_files"] == ["tests/test_local.py"]
    assert module["test_files"] == []
    assert symbol["test_files"] == ["tests/test_local.py"]

    captured_diff = tmp_path / "diff-impact.json"
    captured_diff.write_text(json.dumps(change), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(PLUGIN / "bin") + os.pathsep + os.environ["PATH"])

    rc = test_impact_union.main([str(captured_diff)])

    output = capsys.readouterr()
    assert rc == 0, output.err
    summary = json.loads(output.out)
    assert summary["test_impact"]["test_files"] == ["tests/test_local.py"]
    assert summary["test_impact"]["total"] == 1


def _report_state(tmp_path: Path, state: Path) -> None:
    """Prepare the two sentinels required for a successful scan's report block."""
    (state.parent / f"codemap-scan-ok-{tmp_path.name}-skill-safety").write_text("ok\n", encoding="utf-8")


@needs_git_and_bash
@pytest.mark.parametrize("step", [0, 1])
def test_incomplete_scan_state_cannot_inherit_arguments(tmp_path: Path, shell_env: dict[str, str], step: int) -> None:
    """A missing state field must stop both boundaries even when its variable is inherited."""
    state = _scan_state(tmp_path)
    lines = state.read_text(encoding="utf-8").splitlines()
    state.write_text(
        "\n".join(line for line in lines if not line.startswith("SCAN_ARGS_RAW=")) + "\n", encoding="utf-8"
    )
    _report_state(tmp_path, state)
    env = {**shell_env, "STATE_FILE": str(state), "SCAN_ARGS_RAW": "--root unintended"}

    result = _shell(_SETUP_BOUNDARY + _bash_blocks("scan-codebase")[step], tmp_path, env)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "state incomplete" in result.stdout + result.stderr
    assert not (tmp_path / "dispatcher-args").exists()


@needs_git_and_bash
def test_scan_report_does_not_execute_state(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """The second Bash boundary must also treat swapped state content as data."""
    state = _scan_state(tmp_path)
    _report_state(tmp_path, state)

    _shell(_bash_blocks("scan-codebase")[1], tmp_path, {**shell_env, "STATE_FILE": str(state)})

    assert not (tmp_path / "executed").exists()


@needs_git_and_bash
def test_scan_report_missing_index_fails_with_expected_path(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """An absent index after a zero scanner exit cannot permit the Index ready step."""
    state = _scan_state(tmp_path)
    _report_state(tmp_path, state)

    result = _shell(_bash_blocks("scan-codebase")[1], tmp_path, {**shell_env, "STATE_FILE": str(state)})

    assert result.returncode == 1, result.stdout + result.stderr
    assert str(tmp_path / "index" / "project.json") in result.stdout + result.stderr


@needs_git_and_bash
def test_scan_report_preserves_quoted_custom_root(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Reading state as data preserves the selected root when the reporting shell is new."""
    root = tmp_path / "project's source = literal"
    root.mkdir()
    (root / "app.py").write_text("def sample():\n    return 1\n", encoding="utf-8")
    env = {**shell_env, "CODEMAP_INDEX_DIR": str(tmp_path / ".cache" / "codemap")}
    scan = subprocess.run(
        [sys.executable, str(PLUGIN / "bin" / "scan-index"), "--root", str(root)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert scan.returncode == 0, scan.stdout + scan.stderr
    state = _scan_state(tmp_path, "--root " + shlex.quote(str(root)), root.name)
    _report_state(tmp_path, state)

    result = _shell(_bash_blocks("scan-codebase")[1], tmp_path, {**env, "STATE_FILE": str(state)})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Skipping stats" not in result.stdout
    assert not (tmp_path / "executed").exists()


@needs_git_and_bash
@pytest.mark.parametrize("git_rc", [0, 1])
def test_single_target_scope_counts_other_changes(tmp_path: Path, shell_env: dict[str, str], git_rc: int) -> None:
    """The shipped scope block subtracts the selected module and discloses unavailable Git counts."""
    git_bin = tmp_path / "bin"
    git_bin.mkdir()
    git = git_bin / "git"
    git.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        'assert sys.argv[1:] == ["diff", "--name-only", "HEAD", "--", "*.py"]\n'
        'print("src/pkg/a.py\\nsrc/pkg/b.py\\ntests/test_b.py")\n'
        'sys.exit(int(os.environ["GIT_RC"]))\n',
        encoding="utf-8",
    )
    git.chmod(0o755)
    (tmp_path / f"codemap-{tmp_path.name}-ti-qname-skill-safety").write_text("pkg.a::run\n", encoding="utf-8")
    block = next(block for block in _bash_blocks("test-impact") if "Scope: covers" in block)

    result = _shell(
        "git() { printf '%s\\n' \"$TMPDIR\"; }\n" + block,
        tmp_path,
        {**shell_env, "GIT_RC": str(git_rc), "PATH": str(git_bin) + os.pathsep + os.environ["PATH"]},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    expected = {0: "2 other changed modules not analysed.", 1: "other changed modules not analysed; count unknown"}
    assert "Scope: covers pkg.a::run only; " + expected[git_rc] in result.stdout


@needs_git_and_bash
@pytest.mark.parametrize("step", [0, 1])
def test_unquoted_state_command_is_rejected(tmp_path: Path, shell_env: dict[str, str], step: int) -> None:
    """A non-data command planted in the state must neither execute nor permit later use."""
    state = _scan_state(tmp_path)
    with state.open("a", encoding="utf-8") as stream:
        stream.write('printf exploited > "$TMPDIR/executed"\n')
    _report_state(tmp_path, state)

    result = _shell(
        _SETUP_BOUNDARY + _bash_blocks("scan-codebase")[step], tmp_path, {**shell_env, "STATE_FILE": str(state)}
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "state incomplete" in result.stdout + result.stderr
    assert not (tmp_path / "executed").exists()
    assert not (tmp_path / "dispatcher-args").exists()
