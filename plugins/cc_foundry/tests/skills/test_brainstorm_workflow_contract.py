"""Protect brainstorm decisions, saved artifacts and viewer scope against ambiguous instructions."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_requires_bash = pytest.mark.skipif(not _bash_runs_posix_script(), reason="requires Bash executing POSIX scripts")


def _section(relative: str, start: str, end: str) -> str:
    """Read one instruction region without depending on paragraph wrapping."""
    lines = (_SKILLS / relative).read_text(encoding="utf-8").splitlines()
    first = next(index for index, line in enumerate(lines) if line.startswith(start))
    last = next(index for index in range(first + 1, len(lines)) if lines[index].startswith(end))
    return "\n".join(lines[first:last])


@_requires_bash
@pytest.mark.parametrize("project_name", ["plain", "space and 'quote"])
def test_viewer_setup_exposes_only_copied_viewer_and_live_state(tmp_path: Path, project_name: str) -> None:
    """Run the setup recipe and keep unrelated home files outside its document root."""
    tmp_path = tmp_path / project_name
    tmp_path.mkdir()
    section = _section("brainstorm/SKILL.md", "On **(a)**:", "**Persistence note**")
    script = section.split("```bash\n", 1)[1].split("```", 1)[0]
    plugin = tmp_path / "plugin"
    scripts = plugin / "skills" / "brainstorm" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "tree-viewer.html").write_text("viewer", encoding="utf-8")
    (tmp_path / "private-key").write_text("secret", encoding="utf-8")
    report = ".reports/brainstorm/2026-11-11T10-00-00Z"
    (tmp_path / "brainstorm-state-report-dir-viewer-test").write_text(f"{report}\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-eu", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "TMPDIR": tmp_path.as_posix(),
            "CLAUDE_PLUGIN_ROOT": plugin.as_posix(),
            "CSID": "viewer-test",
            "CLAUDE_CODE_SESSION_ID": "viewer-test",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    sidecar = Path((tmp_path / "brainstorm-state-sidecar-viewer-test").read_text(encoding="utf-8").strip())
    assert sidecar == tmp_path / ".temp/brainstorm/2026-11-11T10-00-00Z/viewer/tree.json"
    assert sidecar.parent != tmp_path
    assert (sidecar.parent / "tree-viewer.html").read_text(encoding="utf-8") == "viewer"
    assert not (sidecar.parent / "tree-viewer.html").is_symlink()
    assert not (sidecar.parent / "private-key").exists()
    launch = _section("brainstorm/SKILL.md", "Print launch note:", "## Step 2")
    assert "```text" in launch
    assert "```bash" not in launch
    assert "export CSID" not in launch
    assert "exit 1" not in launch
    assert "separate terminal" in launch
    assert "stop the server when finished" in launch
    assert "?state=tree.json" in launch
    assert "$HOME" not in launch
    command = next(line for line in result.stdout.splitlines() if line.startswith("python -m http.server"))
    assert "8080 --bind 127.0.0.1 --directory" in command
    parsed = subprocess.run(
        ["bash", "-eu", "-c", "set -- " + command + '; printf "%s\\n" "$@"'],
        cwd=tmp_path,
        env={key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_SESSION_ID"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert parsed.stdout.splitlines() == [
        "python",
        "-m",
        "http.server",
        "8080",
        "--bind",
        "127.0.0.1",
        "--directory",
        sidecar.parent.as_posix(),
    ]
    assert set(path.name for path in sidecar.parent.iterdir()) == {"tree-viewer.html"}
    sidecar.write_text('{"session_status":"active"}\n', encoding="utf-8")
    (scripts / "tree-viewer.html").write_text("replacement", encoding="utf-8")
    collision = subprocess.run(
        ["bash", "-eu", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "TMPDIR": tmp_path.as_posix(),
            "CLAUDE_CODE_SESSION_ID": "viewer-test",
            "CLAUDE_PLUGIN_ROOT": plugin.as_posix(),
        },
        capture_output=True,
        text=True,
    )
    assert collision.returncode != 0
    assert (sidecar.parent / "tree-viewer.html").read_text(encoding="utf-8") == "viewer"
    assert set(path.name for path in sidecar.parent.iterdir()) == {"tree-viewer.html", "tree.json"}
    assert sidecar.read_text(encoding="utf-8") == '{"session_status":"active"}\n'
    assert not (tmp_path / ".plans/blueprint").exists()


@_requires_bash
def test_viewer_opt_out_clears_sidecar_in_a_fresh_shell(tmp_path: Path) -> None:
    """The fixed opt-out recipe clears an earlier viewer path without inherited shell variables."""
    section = _section("brainstorm/SKILL.md", "On **(b)**:", "On **(a)**:")
    script = section.split("```bash\n", 1)[1].split("```", 1)[0]
    assert len(script.splitlines()) == 2
    sentinel = tmp_path / "brainstorm-state-sidecar-opt-out"
    sentinel.write_text("old-viewer/tree.json\n", encoding="utf-8")
    subprocess.run(
        ["bash", "-eu", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": tmp_path.as_posix(), "CLAUDE_CODE_SESSION_ID": "opt-out"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert sentinel.read_text(encoding="utf-8") == "\n"


def test_viewer_completion_names_server_stop_and_normal_artifact_cleanup() -> None:
    """Completion tells the user how to release the viewer and its project-local files."""
    section = _section("brainstorm/SKILL.md", "## Step 6", "## Mode: Breakdown")
    assert "stop the viewer server if you started it" in section
    assert "`.temp/`" in section
    assert "normal artifact cleanup" in section


def test_readme_viewer_recipe_uses_an_isolated_loopback_document_root() -> None:
    """Following the public viewer instructions must not expose the project to the network."""
    lines = (_SKILLS.parent / "README.md").read_text(encoding="utf-8").splitlines()
    recipe = next(line for line in lines if line.startswith("6. Live tree viewer"))
    assert "only `tree-viewer.html` and `tree.json`" in recipe
    assert "python -m http.server 8080 --bind 127.0.0.1 --directory <dir>" in recipe
    assert "http://127.0.0.1:8080/tree-viewer.html?state=tree.json" in recipe
    assert "never the project root" in recipe


@pytest.mark.parametrize("name", ["Deepen", "Reject", "Accept", "Merge", "Add", "Reopen", "Idea stacking", "Ready"])
def test_operations_are_keyed_by_name(name: str) -> None:
    """Operation details cannot override the letters of the two-level category menu."""
    section = _section("brainstorm/SKILL.md", "**Operations**:", "### Tree summary format")
    assert any(line.startswith(f"- **{name}**:") for line in section.splitlines())


def test_question_limits_have_one_definition_and_explicit_gate_references() -> None:
    """Seeding, loop-limit and final gates retain the same bounded question contract."""
    text = (_SKILLS / "brainstorm/SKILL.md").read_text(encoding="utf-8")
    assert sum("6 plain lines" in line for line in text.splitlines()) == 1
    assert sum("§Tree summary format — Question text rule" in line for line in text.splitlines()) == 4
    section = _section("brainstorm/SKILL.md", "### Tree summary format", "### Loop bounds")
    assert "**Question text rule**" in section
    assert "hard cap 1,500" in section
    assert "recommended option second" in section


def test_escape_hatch_counts_toward_four_option_limit() -> None:
    """The escape hatch leaves room for at most three substantive choices."""
    section = _section("brainstorm/SKILL.md", "Rules:", "**Gate**")
    assert "including the escape hatch" in section
    assert "at most 3 substantive choices" in section


def test_save_as_is_overrides_minimum_rejected_count() -> None:
    """Saving at the operation limit remains possible without artificial branch rejection."""
    section = _section("brainstorm/SKILL.md", "### Loop bounds", "## Step 4")
    assert "Save tree as-is" in section
    assert "overrides the minimum rejected-branch count" in section


def test_report_identity_survives_compaction_and_slug_precedes_artifacts() -> None:
    """The run directory is persisted and passed into the boundary contract."""
    setup = _section("brainstorm/SKILL.md", "## Step 0", "## Step 1")
    save = _section("brainstorm/SKILL.md", "## Step 4", "## Step 5")
    build = _section("brainstorm/SKILL.md", "## Step 3", "### Pre-seeding exchange")
    assert 'echo "$REPORT_DIR" > "${TMPDIR:-/tmp}/brainstorm-state-report-dir-${CSID}"' in setup
    assert 'IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/brainstorm-state-report-dir-${CSID}"' in save
    assert '"$_REPORT_DIR" "$_PRESERVE"' in save
    assert "report-dir=$_REPORT_DIR" in save
    assert "kebab-case, max 5 words" in build


@_requires_bash
def test_boundary_contract_records_persisted_report_directory(tmp_path: Path) -> None:
    """Execute both recipes with fresh shells and preserve the original report identity."""
    setup = _section("brainstorm/SKILL.md", "## Step 0", "## Step 1").split("```bash\n", 1)[1].split("```", 1)[0]
    save = _section("brainstorm/SKILL.md", "## Step 4", "## Step 5").split("```bash\n", 1)[1].split("```", 1)[0]
    tree = tmp_path / "tree.md"
    tree.write_text("# Direction\n", encoding="utf-8")
    env = {
        **os.environ,
        "TMPDIR": tmp_path.as_posix(),
        "CLAUDE_PLUGIN_ROOT": _SKILLS.parent.as_posix(),
        "CLAUDE_CODE_SESSION_ID": "contract-test",
        "ARGUMENTS": "a direction",
    }
    setup = setup.replace('python "', f'"{Path(sys.executable).as_posix()}" "')
    save = save.replace('python "', f'"{Path(sys.executable).as_posix()}" "')
    subprocess.run(["bash", "-eu", "-c", setup], cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    subprocess.run(
        ["bash", "-eu", "-c", save.replace("<tree-file-path>", tree.as_posix())],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    report = (tmp_path / "brainstorm-state-report-dir-contract-test").read_text(encoding="utf-8").strip()
    contract = (tmp_path / ".temp/state/skill-contract.md").read_text(encoding="utf-8")
    assert f"- run-dir: {report}" in contract
    assert f"report-dir={report}" in contract
    assert f"tree-file={tree.as_posix()}" in contract


@_requires_bash
@pytest.mark.parametrize("matches", [0, 1, 2])
@pytest.mark.parametrize("sentinel_state", ["missing", "empty"])
def test_boundary_recovers_only_an_unambiguous_report_identity(
    tmp_path: Path, matches: int, sentinel_state: str
) -> None:
    """Recover one matching branch artifact, and block absent or ambiguous run identities."""
    save = _section("brainstorm/SKILL.md", "## Step 4", "## Step 5").split("```bash\n", 1)[1].split("```", 1)[0]
    save = save.replace('python "', f'"{Path(sys.executable).as_posix()}" "')
    tree = tmp_path / "tree.md"
    tree.write_text("# Direction\n", encoding="utf-8")
    sentinel = tmp_path / "brainstorm-state-report-dir-missing-report"
    _prepare_recovery_artifacts(tmp_path, matches, sentinel_state, sentinel)
    result = subprocess.run(
        ["bash", "-eu", "-c", save.replace("<tree-file-path>", tree.as_posix()).replace("<run-slug>", "direction")],
        cwd=tmp_path,
        env={
            **os.environ,
            "TMPDIR": tmp_path.as_posix(),
            "CLAUDE_CODE_SESSION_ID": "missing-report",
            "CLAUDE_PLUGIN_ROOT": _SKILLS.parent.as_posix(),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if matches == 1 else 1)
    _assert_report_recovery(tmp_path, sentinel, matches, result.stdout)


def _prepare_recovery_artifacts(root: Path, matches: int, sentinel_state: str, sentinel: Path) -> None:
    """Arrange branch summaries from matching runs and an unrelated run as a negative control."""
    for index in range(matches):
        report = root / ".reports/brainstorm" / f"2026-11-11T10-00-0{index}Z"
        report.mkdir(parents=True)
        (report / "direction-branches.md").write_text("# Direction\n", encoding="utf-8")
    unrelated = root / ".reports/brainstorm/2026-11-11T11-00-00Z"
    unrelated.mkdir(parents=True)
    (unrelated / "other-branches.md").write_text("# Other\n", encoding="utf-8")
    if sentinel_state == "empty":
        sentinel.write_text("\n", encoding="utf-8")


def _assert_report_recovery(root: Path, sentinel: Path, matches: int, output: str) -> None:
    """Check the persisted contract after recovery or the preserved stop boundary on ambiguity."""
    contract = root / ".temp/state/skill-contract.md"
    if matches == 1:
        expected = ".reports/brainstorm/2026-11-11T10-00-00Z"
        assert sentinel.read_text(encoding="utf-8").strip() == expected
        assert f"- run-dir: {expected}" in contract.read_text(encoding="utf-8")
        assert f"report-dir={expected}" in contract.read_text(encoding="utf-8")
    else:
        assert "report directory sentinel missing or empty" in output
        assert "exactly one" in output
        assert not contract.exists()


def test_spec_always_uses_suffix_and_preserves_existing_files() -> None:
    """Approved specs cannot replace their input tree or an earlier spec."""
    section = _section("brainstorm/modes/breakdown.md", "After all sections approved:", "#### Step D4")
    assert "YYYY-MM-DD-<slug>-spec.md" in section
    assert "never overwrite" in section
    assert "-spec-2.md" in section


def test_unknown_status_stops_and_dependent_question_waits_for_branch_choice() -> None:
    """Unsupported documents stop and branch-dependent questions use an actual selection."""
    dispatch = _section("brainstorm/modes/breakdown.md", "Read file at given path.", "### Distillation mode")
    questions = _section("brainstorm/modes/breakdown.md", "#### Step D2", "#### Step D3")
    assert "Any other or missing status" in dispatch
    assert "stop" in dispatch
    assert "tell the user to re-run with a `Status: tree` or `Status: draft` document" in dispatch
    assert "ask for" not in dispatch
    assert "Ask question 1 alone" in questions
    assert "wait for its answer" in questions
    assert "remaining questions" in questions


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param("Once all 6 sections", "On **(b)**:", id="spec-approval"),
        pytest.param("If any item fails", "On **(a)**:", id="graduation"),
        pytest.param("**When task 1 is model-invocable**", "On **(a)**:", id="shell-plan"),
        pytest.param("**When task 1 is NOT model-invocable**", "On **(a)**:", id="manual-plan"),
    ],
)
def test_recommended_option_occupies_second_display_slot(start: str, end: str) -> None:
    """Display order changes without renaming the choices used by answer handlers."""
    section = _section("brainstorm/modes/breakdown.md", start, end)
    options = [
        line for line in section.splitlines() if line.startswith(("- a)", "- b)", "- c)", "- (a)", "- (b)", "- (c)"))
    ]
    assert "recommended" not in options[0]
    assert "recommended" in options[1]


def test_shell_execution_requires_exact_command_and_never_recommends_install_or_network() -> None:
    """A spec cannot turn an installation into the recommended immediate action."""
    section = _section("brainstorm/modes/breakdown.md", "#### Step B3", "End with")
    assert "Never recommend executing install or network commands" in section
    assert "exact shell command" in section
    assert "explicit selection of (a)" in section
    assert "command exceeds the question-text cap" in section
    assert "complete question would exceed" in section
    assert "omit the full invocation from the question" in section
    assert "`Copy plan`" in section


def test_fix_prompt_preserves_home_paths_and_uses_direct_sentinel_reads() -> None:
    """Suggested repairs preserve path meaning and the sentinel permission boundary."""
    lines = (_SKILLS / "audit/templates/fix-prompt.md").read_text(encoding="utf-8").splitlines()
    path_rule = next(line for line in lines if line.startswith("- Hardcoded user path"))
    variable_rule = next(line for line in lines if line.startswith("- Missing variable declaration"))
    assert "`.claude/path`" not in path_rule
    assert "`/Users/<name>/…`" in path_rule
    assert "`/home/<name>/…`" in path_rule
    assert "`~/…` in prose and unquoted shell" in path_rule
    assert "`$HOME/…` inside double quotes" in path_rule
    assert 'IFS= read -r VAR < "<sentinel-path>"' in variable_rule
    assert 'VAR="$(command)"' not in variable_rule
    assert "2>/dev/null || VAR=<default>" in variable_rule
    assert "immediately after" in variable_rule
    assert "`export CSID=…` line (add that line first if absent)" in variable_rule


def test_similarity_report_prefers_efficiency_without_duplicating_live_check_output() -> None:
    """Per-plugin efficiency output takes precedence over the still-active non-efficiency producer."""
    section = _section("audit/templates/report-template.md", "### Code Block Similarity", "```markdown")
    assert "$RUN_DIR/efficiency-check33-*.md" in section
    assert "$RUN_DIR/similarity-check33.md" in section
    assert "each matching file" in section
    assert "non-efficiency Check 33 output" in section
    assert "checks-skills-duplication.md" in section
    assert "take precedence" in section
    assert "not embedded twice" in section
    assert "legacy" not in section
    lines = (_SKILLS / "audit/templates/report-template.md").read_text(encoding="utf-8").splitlines()
    omission = next(line for line in lines if line.startswith("Omit section only if"))
    assert "non-efficiency Check 33 files" in omission
    assert "legacy" not in omission
