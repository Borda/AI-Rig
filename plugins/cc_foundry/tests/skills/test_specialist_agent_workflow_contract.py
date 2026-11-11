"""Protect specialist review, execution and handoff boundaries in shipped agent instructions."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_AGENTS = Path(__file__).resolve().parents[2] / "agents"
_requires_bash = pytest.mark.skipif(not _bash_runs_posix_script(), reason="requires Bash executing POSIX scripts")


def _section(agent: str, start: str, end: str) -> list[str]:
    """Read an instruction section as lines without relying on paragraph wrapping."""
    lines = (_AGENTS / f"{agent}.md").read_text(encoding="utf-8").splitlines()
    first = next(index for index, line in enumerate(lines) if line.startswith(start))
    last = next(index for index in range(first + 1, len(lines)) if lines[index].startswith(end))
    return lines[first:last]


def test_lint_review_does_not_rewrite_the_invoking_tree() -> None:
    """Require report-only review and isolate hooks that may modify files."""
    lines = _section("linting-expert", "<workflow>", "</workflow>")
    assert any("Review mode" in line and "report only" in line for line in lines)
    assert any("disposable copy" in line and "absolute paths" in line for line in lines)
    assert any("Fix mode only" in line and "safe" in line for line in lines)
    assert any("Review mode" in line and "suggestions" in line for line in lines)


def test_lint_pinned_hooks_take_precedence_over_fallback_runners() -> None:
    """Prevent a missing PATH executable from bypassing a configured pinned hook."""
    lines = _section("linting-expert", "<workflow>", "</workflow>")
    assert any("pre-commit run <hook-id> --files <targets>" in line for line in lines)
    assert any("pinned hook" in line and "do not bypass" in line for line in lines)
    assert any(".venv" in line and "uv run --no-sync" in line for line in lines)
    assert any('"status": "blocked"' in line and "required" in line for line in lines)
    assert not any("pip install <tool>" in line for line in lines)


def test_lint_review_copy_is_an_independent_git_root() -> None:
    """Make rewriting pinned hooks runnable without reaching the invoking checkout."""
    lines = _section("linting-expert", "<workflow>", "2.")
    recipe = next(line for line in lines if "lint-review-${CSID}" in line)
    assert "${TMPDIR:-/tmp}/lint-review-${CSID}" in recipe
    assert "outside the repository" in recipe
    assert "git init -q" in recipe
    assert ".pre-commit-config.yaml" in recipe
    assert "tool configuration files" in recipe
    assert "targets" in recipe
    assert "pre-commit run <hook-id> --files <targets>" in recipe
    assert "working directory" in recipe
    assert any("no rewrite flag" in line and "Skip the copy" in line for line in lines)


@pytest.mark.parametrize("step", ["7.", "8."])
def test_lint_configuration_changes_require_explicit_setup_scope(step: str) -> None:
    """Keep per-file ignores and hook installation out of ordinary review and fix runs."""
    lines = _section("linting-expert", "<workflow>", "</workflow>")
    instruction = next(line for line in lines if line.startswith(step))
    assert "explicit" in instruction
    assert "request" in instruction


def test_lint_followup_stays_targeted_and_routes_local_reviews() -> None:
    """Avoid broad rewriting followups and PR-only routing for local changes."""
    lines = _section("linting-expert", "<notes>", "</notes>")
    followup = next(line for line in lines if line.startswith("**Follow-up**"))
    assert "--all-files" not in followup
    assert "--files <targets>" in followup
    assert "/develop:review" in followup


def test_doc_release_delegation_is_supported_without_owning_release_policy() -> None:
    """Allow mechanical changelog and contributor work dispatched by the release skill."""
    lines = _section("doc-scribe", "<routing-boundaries>", "</routing-boundaries>")
    assert any("/oss:release" in line and "Exception" in line and "contributors" in line for line in lines)
    assert any("oss:shepherd" in line and "lifecycle" in line for line in lines)


@pytest.mark.parametrize("agent", ["doc-scribe", "linting-expert"])
def test_documentation_cleanup_handoff_excludes_markdown_prose(agent: str) -> None:
    """Keep README formatting with its documentation owner and Python cleanup with linting."""
    lines = _section(agent, "<notes>", "</notes>")
    assert any("Python docstrings" in line and "examples" in line for line in lines)
    assert any("Markdown" in line and "doc-scribe" in line for line in lines)


def test_doc_examples_and_execution_exception_are_bounded() -> None:
    """Require public examples when writing and a side-effect-aware single doctest exception."""
    checks = _section("doc-scribe", "<quality-checks>", "</quality-checks>")
    workflow = _section("doc-scribe", "<workflow>", "</workflow>")
    assert "- At least one `Examples` section per public function" in checks
    assert any("exception" in line and "side effects" in line and "doctest" in line for line in workflow)


def test_doc_self_evident_example_exception_applies_only_to_audits() -> None:
    """Keep the audit false-positive exception separate from the writing checklist."""
    lines = _section("doc-scribe", "## False Positive Traps", "</antipatterns-to-flag>")
    assert any(
        "when auditing" in line.lower() and "do not flag absent Examples on self-evident functions" in line
        for line in lines
    )


def test_lint_severity_precedes_rule_category() -> None:
    """Prevent security findings from sorting below style by category order."""
    lines = _section("linting-expert", "<output-format>", "</output-format>")
    assert any("critical → high → medium → low" in line and "rule category" in line for line in lines)
    assert not any("Group findings by severity tier" in line for line in lines)


def test_perf_advisory_branch_stops_before_runtime_and_implementation() -> None:
    """Keep experiment-plan reviews static while permitting a mandated review artifact."""
    lines = _section("perf-optimizer", "<workflow>", "</workflow>")
    gate = next(index for index, line in enumerate(lines) if line.startswith("0."))
    measurement = next(index for index, line in enumerate(lines) if line.startswith("1."))
    assert gate < measurement
    assert "static analysis only" in lines[gate]
    assert "do not execute" in lines[gate]
    assert "do not edit" in lines[gate]
    assert "requested review artifact" in lines[gate]
    assert "step 6" in lines[gate]
    assert "run 1a, 2 (static) and 4" in lines[gate]
    assert "skip 1b, 3 and 5" in lines[gate]


def test_perf_implementation_skip_requires_no_performance_complaint() -> None:
    """Keep performance-motivated implementation requests within the optimizer's scope."""
    lines = _section("perf-optimizer", "<routing-boundaries>", "</routing-boundaries>")
    skip = next(line for line in lines if line.startswith("- SKIP also:"))
    assert "general implementation task with no performance complaint present" in skip


def test_perf_profiling_collects_foreground_results() -> None:
    """Require bounded foreground profiling so spawned agents receive their measurements."""
    lines = _section("perf-optimizer", "3. **Profile", "4. **Fill")
    assert any("foreground Bash" in line and "timeout" in line for line in lines)
    assert not any("run_in_background: true" in line for line in lines)


def test_perf_refuses_untracked_targets_and_protects_concurrent_edits() -> None:
    """Prevent an incomplete checkpoint from losing user files or concurrent changes."""
    lines = _section("perf-optimizer", "5. **One-change", "5a.")
    assert any("untracked or ignored" in line and "do not edit" in line for line in lines)
    assert any("exact paths" in line and "before" in line for line in lines)
    assert any("candidate" in line and "concurrent" in line and "stop" in line for line in lines)
    assert not any("git diff --name-only" in line and "git restore" in line for line in lines)


def test_perf_baseline_uses_checked_concrete_script_path() -> None:
    """Keep unresolved angle-bracket placeholders out of executable baseline commands."""
    lines = _section("perf-optimizer", "### 1b.", "### 1c.")
    assert not any("<module>" in line or "<script.py>" in line for line in lines)
    assert any("WORKLOAD_SCRIPT:?" in line for line in lines)
    assert any('python "$WORKLOAD_SCRIPT"' in line for line in lines)


def test_perf_static_output_does_not_invent_measured_impact() -> None:
    """Require unmeasured impact as well as unmeasured timing in advisory reports."""
    lines = _section("perf-optimizer", "<output-format>", "</output-format>")
    assert any("[Impact]" in line and "unconfirmed" in line and "Static-only" in line for line in lines)
    assert any("labelled estimate" in line and "static evidence" in line for line in lines)
    assert any("do not invent numerical gains" in line for line in lines)


def test_perf_rollback_scope_excludes_renames() -> None:
    """Avoid prescribing restoration of untracked rename destinations."""
    lines = _section("perf-optimizer", "5. **One-change", "5a.")
    assert any("Renames are out of scope" in line for line in lines)
    assert not any("no-renames" in line or "renamed file requires both paths" in line for line in lines)


def _baseline_prefix() -> str:
    """Extract the executable timing recipe before optional GPU monitoring."""
    lines = _section("perf-optimizer", "### 1b.", "### 1c.")
    first = lines.index("```bash") + 1
    last = next(index for index in range(first, len(lines)) if lines[index].startswith("# nvidia-smi:"))
    return "\n".join(lines[first:last])


@_requires_bash
@pytest.mark.integration
@pytest.mark.parametrize("extra_env", [pytest.param({}, id="unset"), pytest.param({"WORKLOAD_SCRIPT": ""}, id="empty")])
def test_perf_baseline_rejects_unset_script_before_python(extra_env: dict[str, str], tmp_path: Path) -> None:
    """Execute the real recipe and fail explicitly before any workload can run."""
    env = {key: value for key, value in os.environ.items() if key != "WORKLOAD_SCRIPT"}
    result = subprocess.run(
        ["bash", "-c", _baseline_prefix()], cwd=tmp_path, env=env | extra_env, capture_output=True, encoding="utf-8"
    )
    assert result.returncode != 0
    assert "WORKLOAD_SCRIPT" in result.stderr
    assert "Set WORKLOAD_SCRIPT" in result.stderr
    assert list(tmp_path.iterdir()) == []


@_requires_bash
@pytest.mark.integration
def test_perf_baseline_rejects_nonexistent_script(tmp_path: Path) -> None:
    """Reject a supplied missing path without interpreting shell redirect placeholders."""
    result = subprocess.run(
        ["bash", "-c", _baseline_prefix()],
        cwd=tmp_path,
        env=os.environ | {"WORKLOAD_SCRIPT": str(tmp_path / "absent.py")},
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 1
    assert "BLOCKED — workload script not found" in result.stdout
    assert list(tmp_path.iterdir()) == []


@_requires_bash
@pytest.mark.integration
def test_perf_baseline_runs_script_with_spaces(tmp_path: Path) -> None:
    """Pass a verified path as one argument to the project Python interpreter."""
    script = tmp_path / "representative workload.py"
    script.write_text('from pathlib import Path\nPath("measured.txt").write_text("ran")\n', encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _baseline_prefix()],
        cwd=tmp_path,
        env=os.environ
        | {"WORKLOAD_SCRIPT": str(script), "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "measured.txt").read_text(encoding="utf-8") == "ran"


@_requires_bash
@pytest.mark.integration
@pytest.mark.parametrize("harness", [False, True])
def test_perf_baseline_preserves_workload_arguments(tmp_path: Path, harness: bool) -> None:
    """Time scripts and external function harnesses with the same argument boundaries."""
    session = tmp_path / "session"
    session.mkdir()
    script = session / "perf-workload-test.py" if harness else tmp_path / "training script.py"
    (tmp_path / "workload.py").write_text("def run(config):\n    return 'ran ' + config\n", encoding="utf-8")
    script.write_text(
        "import sys\nfrom workload import run\n"
        "assert sys.argv[1:] == ['--config', 'config with spaces.yaml']\n"
        "assert run(sys.argv[2]) == 'ran config with spaces.yaml'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            'WORKLOAD_ARGS=("$@");\n' + _baseline_prefix(),
            "baseline",
            "--config",
            "config with spaces.yaml",
        ],
        cwd=tmp_path,
        env=os.environ
        | {
            "WORKLOAD_SCRIPT": str(script),
            "PYTHONPATH": str(tmp_path),
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        },
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_perf_baseline_documents_arguments_and_external_harness() -> None:
    """Keep the script recipe usable for function workloads without repository edits."""
    lines = _section("perf-optimizer", "### 1b.", "### 1c.")
    assert any(
        "${TMPDIR:-/tmp}/perf-workload-${CSID}.py" in line and "outside the repository" in line for line in lines
    )
    assert any("WORKLOAD_ARGS" in line and "Bash array" in line and "same shell invocation" in line for line in lines)
    assert sum('python "$WORKLOAD_SCRIPT" "${WORKLOAD_ARGS[@]}"' in line for line in lines) == 2


def _iteration_recipe(start: str) -> str:
    """Extract one executable iteration block without relying on surrounding prose wrapping."""
    lines = _section("perf-optimizer", start, "5a.")
    first = lines.index("```bash") + 1
    last = lines.index("```", first)
    return "\n".join(lines[first:last])


_GIT_RESTORE_BOUNDARY = """
git() {
    case "$1" in
        hash-object) command git "$@" ;;
        stash) printf '%s\\n' captured-ref ;;
        restore) printf '%s\\0' "$@" > "$RESTORE_LOG" ;;
        ls-files) return 0 ;;
        *) return 91 ;;
    esac
}
"""


@pytest.fixture
def perf_candidate(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Prepare isolated candidate files and checkpoint records without any Git writes."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    session = tmp_path / "session"
    session.mkdir()
    paths = ["module with spaces.py", "owned.py"]
    (checkout / paths[0]).write_text("candidate one\n", encoding="utf-8")
    (checkout / paths[1]).write_text("candidate two\n", encoding="utf-8")
    (checkout / "unrelated.py").write_text("parallel edit\n", encoding="utf-8")
    (session / "perf-paths-test").write_bytes(b"module with spaces.py\0owned.py\0")
    (session / "perf-checkpoint-ref-test").write_bytes(b"checkpoint-ref\n")
    (session / "perf-checkpoint-untracked-test").write_bytes(b"")
    hashes = subprocess.run(["git", "hash-object", "--", *paths], cwd=checkout, capture_output=True, check=True).stdout
    (session / "perf-candidate-test").write_bytes(hashes)
    env = os.environ | {
        "TMPDIR": str(session),
        "CLAUDE_CODE_SESSION_ID": "test",
        "RESTORE_LOG": str(session / "restore-args"),
    }
    return checkout, env


@_requires_bash
def test_perf_candidate_capture_hashes_recorded_paths(perf_candidate: tuple[Path, dict[str, str]]) -> None:
    """Record actual candidate hashes using the path list saved by the checkpoint call."""
    checkout, env = perf_candidate
    candidate = Path(env["TMPDIR"]) / "perf-candidate-test"
    expected = candidate.read_bytes()
    candidate.unlink()
    result = subprocess.run(
        ["bash", "-c", _GIT_RESTORE_BOUNDARY + _iteration_recipe("After editing")],
        cwd=checkout,
        env=env,
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert candidate.read_bytes() == expected
    assert not Path(env["RESTORE_LOG"]).exists()


@_requires_bash
def test_perf_checkpoint_persists_paths_and_invalidates_previous_candidate(
    perf_candidate: tuple[Path, dict[str, str]],
) -> None:
    """Save the exact next iteration's ownership before editing while preserving current files."""
    checkout, env = perf_candidate
    session = Path(env["TMPDIR"])
    (session / "perf-paths-test").unlink()
    result = subprocess.run(
        [
            "bash",
            "-c",
            _GIT_RESTORE_BOUNDARY + 'PERF_PATHS=("$@");\n' + _iteration_recipe("Checkpoint before"),
            "checkpoint",
            "module with spaces.py",
            "owned.py",
        ],
        cwd=checkout,
        env=env,
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert (session / "perf-paths-test").read_bytes() == b"module with spaces.py\0owned.py\0"
    assert (session / "perf-checkpoint-ref-test").read_bytes() == b"captured-ref\n"
    assert (session / "perf-candidate-test").read_bytes() == b""
    assert (checkout / "module with spaces.py").read_text(encoding="utf-8") == "candidate one\n"
    assert (checkout / "owned.py").read_text(encoding="utf-8") == "candidate two\n"
    assert (checkout / "unrelated.py").read_text(encoding="utf-8") == "parallel edit\n"


@_requires_bash
def test_perf_rollback_reloads_ref_and_restores_only_recorded_paths(
    perf_candidate: tuple[Path, dict[str, str]],
) -> None:
    """Check real candidate hashes before handing the exact owned path list to Git restore."""
    checkout, env = perf_candidate
    result = subprocess.run(
        ["bash", "-c", _GIT_RESTORE_BOUNDARY + _iteration_recipe("On regression")],
        cwd=checkout,
        env=env,
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert Path(env["RESTORE_LOG"]).read_bytes().split(b"\0") == [
        b"restore",
        b"--source=checkpoint-ref",
        b"--worktree",
        b"--",
        b"module with spaces.py",
        b"owned.py",
        b"",
    ]
    assert (checkout / "unrelated.py").read_text(encoding="utf-8") == "parallel edit\n"


@_requires_bash
@pytest.mark.parametrize("changed", ["module with spaces.py", "owned.py"])
def test_perf_rollback_stops_on_concurrent_edits(perf_candidate: tuple[Path, dict[str, str]], changed: str) -> None:
    """Stop before any restore when either owned file diverges from its recorded candidate."""
    checkout, env = perf_candidate
    (checkout / changed).write_text("concurrent edit\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _GIT_RESTORE_BOUNDARY + _iteration_recipe("On regression")],
        cwd=checkout,
        env=env,
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 1
    assert "BLOCKED" in result.stdout
    assert not Path(env["RESTORE_LOG"]).exists()
    assert (checkout / changed).read_text(encoding="utf-8") == "concurrent edit\n"


@_requires_bash
@pytest.mark.parametrize("record", ["perf-checkpoint-ref-test", "perf-paths-test", "perf-candidate-test"])
def test_perf_rollback_stops_without_checkpoint_records(
    perf_candidate: tuple[Path, dict[str, str]], record: str
) -> None:
    """Refuse incomplete rollback records rather than guessing a ref or a file set."""
    checkout, env = perf_candidate
    (Path(env["TMPDIR"]) / record).unlink()
    result = subprocess.run(
        ["bash", "-c", _GIT_RESTORE_BOUNDARY + _iteration_recipe("On regression")],
        cwd=checkout,
        env=env,
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 1
    assert "BLOCKED" in result.stdout
    assert not Path(env["RESTORE_LOG"]).exists()
