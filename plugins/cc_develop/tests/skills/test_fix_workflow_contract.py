"""Exercise fix workflow recipes at issue, target, diagnosis, and review boundaries."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/fix/SKILL.md"
_BASH = shutil.which("bash")
_SESSION = "fix-workflow"
_requires_bash = pytest.mark.skipif(_BASH is None, reason="documented recipes require bash")


def _block(marker: str, *, dedent: bool = True) -> str:
    """Read a shipped fence by its command marker, retaining nested list indentation semantics."""
    blocks = []
    body: list[str] | None = None
    fence = ""
    for line in _SKILL.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if body is None and stripped.endswith("bash") and stripped.startswith("```"):
            fence = stripped.removesuffix("bash")
            body = []
        elif body is not None and stripped == fence:
            blocks.append(textwrap.dedent("\n".join(body)) if dedent else "\n".join(body))
            body = None
        elif body is not None:
            body.append(line)
    return next(block for block in blocks if marker in block)


def _run(block: str, tmp_path: Path, setup: str = "", arguments: str = "") -> subprocess.CompletedProcess[str]:
    """Run a recipe with private session files and the current Python interpreter."""
    env = {
        **os.environ,
        "TMPDIR": tmp_path.as_posix(),
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "CSID": _SESSION,
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        "ARGUMENTS": arguments,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    python = f'python() {{ {shlex.quote(sys.executable)} "$@"; }}\n'
    return subprocess.run(
        [_BASH, "-c", python + setup + "\n" + block],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@_requires_bash
@pytest.mark.parametrize(
    ("argument", "fetch"),
    [
        pytest.param("123", True, id="issue"),
        pytest.param("#123", True, id="hash-issue"),
        pytest.param("500 error on /login", False, id="numeric-symptom"),
        pytest.param("3 tests fail", False, id="test-count"),
        pytest.param("123abc", False, id="numeric-prefix"),
        pytest.param("#12 extra", False, id="hash-prefix"),
    ],
)
def test_only_whole_issue_numbers_fetch(tmp_path: Path, argument: str, fetch: bool) -> None:
    """A numeric prefix in a symptom never fetches an unrelated issue."""
    (tmp_path / f"dev-fix-clean-args-{_SESSION}").write_text(argument + "\n", encoding="utf-8")
    result = _run(_block("dev_issue_fetch_wrap.py"), tmp_path, "python() { echo ISSUE_FETCH; }")
    assert result.returncode == 0, result.stderr
    assert ("ISSUE_FETCH" in result.stdout) is fetch


@_requires_bash
@pytest.mark.parametrize(
    "marker",
    [
        "TEST_PATH=",
        "CANDIDATE_EXIT=$?",
        "GATE_P1=$?",
        "GATE_P2=$?",
        "Outcome B gate not satisfied",
        "<test_file>::<test_name>",
    ],
)
def test_unresolved_targets_abort_before_launch(tmp_path: Path, marker: str) -> None:
    """An unedited recipe stops explicitly before any runner or shell redirection can consume a stand-in."""
    (tmp_path / f"dev-pytest-cmd-{_SESSION}").write_text("pytest\n", encoding="utf-8")
    result = _run(_block(marker), tmp_path, "python() { echo RUNNER_LAUNCHED; }")
    assert result.returncode == 1, result.stderr
    assert "RUNNER_LAUNCHED" not in result.stdout
    assert "unresolved" in result.stdout
    assert "syntax error" not in result.stderr


@_requires_bash
@pytest.mark.parametrize(
    ("status", "continues"),
    [
        pytest.param(0, True, id="passed"),
        pytest.param(1, False, id="regression-remains"),
        pytest.param(4, False, id="usage-error"),
        pytest.param(5, False, id="no-tests"),
        pytest.param(64, False, id="runner-refused"),
    ],
)
def test_regression_run_only_continues_on_zero(tmp_path: Path, status: int, continues: bool) -> None:
    """Step 3 cannot continue to affected-test selection after a failed or unexecuted regression."""
    block = _block("<test_file>::<test_name>")
    block = block.replace("<test_file>::<test_name>", "tests/test_leaf.py::test_leaf")
    result = _run(block + "\necho CONTINUED", tmp_path, f"python() {{ return {status}; }}")
    assert (result.returncode, "CONTINUED" in result.stdout) == (0 if continues else 1, continues)


def _diagnosis(tmp_path: Path, qname: str, *, stale: bool | None = False) -> tuple[Path, str]:
    """Write an unchanged-index diagnosis and its matching private index."""
    diagnosis = tmp_path / "diagnosis.md"
    payload = {
        "qname": qname,
        "pytest_cmd": "pytest tests/test_leaf.py",
        "test_files": ["tests/test_leaf.py"],
        "test_modules": ["tests.test_leaf"],
        "via_call": 1,
        "via_mock": 0,
        "total": 1,
        "index": {"query_complete": True, "root_mismatch": False, "compact": True, "scope": "test-impact"},
    }
    if stale is not None:
        payload["index"]["stale"] = stale
    diagnosis.write_text(
        "## Test Impact (codemap-py)\n- index_scanned_at: unchanged\n```json\n" + json.dumps(payload) + "\n```\n",
        encoding="utf-8",
    )
    index = tmp_path / "index"
    index.mkdir()
    (index / f"{tmp_path.name}.json").write_text('{"scanned_at":"unchanged"}', encoding="utf-8")
    (tmp_path / f"dev-fix-diag-file-{_SESSION}").write_text(diagnosis.as_posix() + "\n", encoding="utf-8")
    setup = f"export CODEMAP_INDEX_DIR={shlex.quote(index.as_posix())}\n"
    return diagnosis, setup


@_requires_bash
@pytest.mark.parametrize(
    ("changed", "untracked", "qname", "reuse"),
    [
        pytest.param("leaf.py", "", "leaf", True, id="covered-module"),
        pytest.param("src/pkg/leaf.py", "", "pkg.leaf", True, id="src-layout"),
        pytest.param("lib/pkg/__init__.py", "", "pkg", True, id="lib-package"),
        pytest.param("leaf.py\nother.py", "", "leaf", False, id="extra-module"),
        pytest.param("leaf.py", "other.py", "leaf", False, id="untracked-module"),
        pytest.param("leaf.py", "tests/test_new.py", "leaf", False, id="new-regression"),
        pytest.param("leaf.py", "", "leaf::one_function", False, id="function-only-coverage"),
        pytest.param("", "", "leaf", False, id="no-changed-module"),
        pytest.param("leaf.py", "", "", False, id="unknown-coverage"),
    ],
)
def test_handoff_reuse_covers_every_changed_path(
    tmp_path: Path, changed: str, untracked: str, qname: str, reuse: bool
) -> None:
    """Index freshness alone cannot reuse a diagnosis test set for changes beyond its queried module."""
    diagnosis, setup = _diagnosis(tmp_path, qname)
    setup += (
        "git() { case \"$1\" in rev-parse) pwd;; diff) printf '%s\\n' "
        + shlex.quote(changed)
        + ";; ls-files) printf '%s\\n' "
        + shlex.quote(untracked)
        + ";; esac; }\n"
    )
    result = _run(_block('REUSED_PYTEST_CMD=""'), tmp_path, setup, f"--diagnosis {diagnosis.as_posix()}")
    assert result.returncode == 0, result.stderr
    selected = (tmp_path / f"dev-fix-reused-pytest-cmd-{_SESSION}").read_text(encoding="utf-8").strip()
    assert selected == ("pytest tests/test_leaf.py" if reuse else "")


@_requires_bash
@pytest.mark.parametrize("stale", [True, None])
def test_handoff_requires_explicit_freshness(tmp_path: Path, stale: bool | None) -> None:
    """Stale or unknown nested index freshness clears a previous reused command despite matching module coverage."""
    diagnosis, setup = _diagnosis(tmp_path, "leaf", stale=stale)
    sentinel = tmp_path / f"dev-fix-reused-pytest-cmd-{_SESSION}"
    sentinel.write_text("pytest tests/test_old.py\n", encoding="utf-8")
    setup += 'git() { case "$1" in rev-parse) pwd;; diff) echo leaf.py;; ls-files) :;; esac; }\n'
    result = _run(_block('REUSED_PYTEST_CMD=""'), tmp_path, setup, f"--diagnosis {diagnosis.as_posix()}")
    assert result.returncode == 0, result.stderr
    assert sentinel.read_text(encoding="utf-8").strip() == ""
    assert "re-querying live" in result.stdout
    assert "changed files exceed module coverage" not in result.stdout


@_requires_bash
def test_handoff_reuse_runs_with_list_indentation(tmp_path: Path) -> None:
    """A copied list-indented recipe still reuses fresh module coverage without silently swallowing Python errors."""
    _, setup = _diagnosis(tmp_path, "leaf")
    setup += 'git() { case "$1" in rev-parse) pwd;; diff) echo leaf.py;; ls-files) :;; esac; }\n'
    result = _run(_block('REUSED_PYTEST_CMD=""', dedent=False), tmp_path, setup)
    assert result.returncode == 0, result.stderr
    assert "reusing test-impact" in result.stdout
    assert (tmp_path / f"dev-fix-reused-pytest-cmd-{_SESSION}").read_text().strip() == "pytest tests/test_leaf.py"


@_requires_bash
def test_rejected_diagnosis_clears_previous_path(tmp_path: Path) -> None:
    """A failed parse cannot leave a previous same-session diagnosis available to test selection."""
    _diagnosis(tmp_path, "leaf")
    result = _run(_block("DIAG_FILE=$(python"), tmp_path, arguments="--diagnosis missing.md")
    assert result.returncode == 1, result.stderr
    assert "! BREAKING" in result.stdout
    assert (tmp_path / f"dev-fix-diag-file-{_SESSION}").read_text(encoding="utf-8").strip() == ""


@_requires_bash
def test_diagnosis_path_survives_worktree_directory_change(tmp_path: Path) -> None:
    """A validated relative main-tree diagnosis is persisted absolutely and read from an isolated directory."""
    diagnosis, setup = _diagnosis(tmp_path, "leaf")
    initial = _run(_block("DIAG_FILE=$(python"), tmp_path, arguments="--diagnosis diagnosis.md")
    assert initial.returncode == 0, initial.stderr
    persisted = (tmp_path / f"dev-fix-diag-file-{_SESSION}").read_text(encoding="utf-8").strip()
    assert persisted == diagnosis.as_posix()
    worktree = tmp_path / "isolated"
    worktree.mkdir()
    (tmp_path / "index/isolated.json").write_text('{"scanned_at":"unchanged"}', encoding="utf-8")
    setup += 'git() { case "$1" in rev-parse) pwd;; diff) echo leaf.py;; ls-files) :;; esac; }\n'
    reused = _run(
        _block('REUSED_PYTEST_CMD=""'),
        tmp_path,
        setup + f"cd {shlex.quote(worktree.as_posix())}\n",
        arguments="--diagnosis diagnosis.md",
    )
    assert reused.returncode == 0, reused.stderr
    assert "diagnosis file unavailable" not in reused.stdout
    assert (tmp_path / f"dev-fix-reused-pytest-cmd-{_SESSION}").read_text().strip() == "pytest tests/test_leaf.py"


@_requires_bash
@pytest.mark.parametrize(
    "route", ["skip", "callers", "blast", "dependencies", "test-impact", "coupling", "central", "standard"]
)
def test_fixed_query_route_is_persisted(tmp_path: Path, route: str) -> None:
    """Selecting a fixed route records that exact choice rather than silently defaulting another route."""
    block = _block(f'--query-kind {route} -- "$ARGUMENTS"')
    result = _run(block, tmp_path, arguments="pkg.leaf::run")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / f"dev-fix-codemap-query-kind-{_SESSION}").read_text().strip() == route


@_requires_bash
@pytest.mark.integration
def test_real_regression_must_pass_before_continuation(tmp_path: Path) -> None:
    """The shipped wrapper and real pytest stop on a failing test, then continue after its behavior is corrected."""
    target = tmp_path / "test_leaf.py"
    target.write_text("def test_leaf():\n    assert False\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -p no:xdist\n", encoding="utf-8")
    (tmp_path / f"dev-pytest-cmd-{_SESSION}").write_text("python -m pytest\n", encoding="utf-8")
    block = _block("<test_file>::<test_name>").replace("<test_file>::<test_name>", "test_leaf.py::test_leaf")
    failed = _run(block + "\necho CONTINUED", tmp_path)
    assert (failed.returncode, "CONTINUED" in failed.stdout) == (1, False), failed.stderr
    target.write_text("def test_leaf():\n    assert True\n", encoding="utf-8")
    passed = _run(block + "\necho CONTINUED", tmp_path)
    assert (passed.returncode, "CONTINUED" in passed.stdout) == (0, True), (passed.stdout, passed.stderr)


@_requires_bash
def test_boundary_includes_new_untracked_test(tmp_path: Path) -> None:
    """The review boundary preserves new tests as well as tracked source changes."""
    block = _block("# boundary 2:")
    setup = (
        'git() { case "$1" in diff) echo leaf.py;; ls-files) echo tests/test_new.py;; esac; }\n'
        'python() { case "$1" in */write_skill_contract.py) printf "%s\\n" "$@";; esac; }\n'
    )
    result = _run(block, tmp_path, setup)
    assert result.returncode == 0, result.stderr
    assert "changed-files=leaf.py tests/test_new.py" in result.stdout


@_requires_bash
def test_latest_tagged_suspect_wins_over_rejected_and_test_nodes(tmp_path: Path) -> None:
    """A revision uses the latest approved suspect, ignoring incidental node IDs and earlier suspects."""
    checkpoint = tmp_path / "checkpoint.md"
    checkpoint.write_text(
        "suspect: old.module::old\ntests.test_leaf::test_leaf\nsuspect: new.module::run\n", encoding="utf-8"
    )
    (tmp_path / f"dev-fix-dev-dir-{_SESSION}").write_text(tmp_path.as_posix() + "\n", encoding="utf-8")
    (tmp_path / f"dev-fix-codemap-enabled-{_SESSION}").write_text("true\n", encoding="utf-8")
    setup = 'python() { :; }\ncodemap-py() { printf "%s\\n" "$@"; }\n'
    result = _run(_block("DERIVED_FN="), tmp_path, setup)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "fn-rdeps-output.txt").read_text(encoding="utf-8").splitlines()[-2:] == [
        "new.module::run",
        "--exclude-tests",
    ]


def test_performance_redirect_names_an_agent() -> None:
    """Performance cases route to the installed specialist agent rather than a nonexistent slash command."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    routes = [line for line in lines if "perf-optimizer" in line]
    assert len(routes) == 3
    assert all(
        "`foundry:perf-optimizer`" in line and "agent" in line and "requires foundry plugin" in line for line in routes
    )
