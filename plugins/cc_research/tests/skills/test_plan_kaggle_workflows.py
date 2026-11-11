"""Exercise planning and Kaggle instructions across fresh shells and explicit decision gates.

Run recipes in temporary projects with local command doubles, keeping credentials and network out of the tests.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]


def _lines(skill: str) -> list[str]:
    """Read a skill without depending on paragraph wrapping.

    Contract checks retain individual instruction lines so paragraph boundaries cannot create accidental matches.
    """
    return (_PLUGIN / f"skills/{skill}/SKILL.md").read_text(encoding="utf-8").splitlines()


def _block(skill: str, marker: str) -> str:
    """Find the executable block containing a distinctive command.

    Shell regressions execute the shipped block verbatim instead of maintaining a second copy of its logic.
    """
    current: list[str] | None = None
    for line in _lines(skill):
        if line == "```bash":
            current = []
        elif line == "```" and current is not None:
            body = "\n".join(current)
            if marker in body:
                return body
            current = None
        elif current is not None:
            current.append(line)
    raise AssertionError(f"Missing {skill} block: {marker}")


@pytest.fixture(
    params=[
        pytest.param("bash", marks=pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")),
        pytest.param("zsh", marks=pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh unavailable")),
    ]
)
def shell(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, dict[str, str]]:
    """Prepare a fresh shell with isolated session files and the current interpreter.

    Each shell receives its own temporary project and sentinel directory, preserving normal recipe rehydration.
    """
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    env = os.environ.copy()
    env.update(
        TMPDIR=str(sentinels),
        CLAUDE_CODE_SESSION_ID="workflow-test",
        CLAUDE_PLUGIN_ROOT=str(_PLUGIN),
        TEST_PYTHON=sys.executable,
        TEST_SHELL=request.param,
    )
    return tmp_path, env


def _run(block: str, shell: tuple[Path, dict[str, str]], prelude: str = "") -> subprocess.CompletedProcess[str]:
    """Execute shipped instructions without inheriting state from an earlier Bash call.

    The selected shell starts separately for each block; command doubles replace only external tools in that invocation.
    """
    cwd, env = shell
    return subprocess.run(
        [env["TEST_SHELL"], "-c", 'python() { "$TEST_PYTHON" "$@"; }\n' + prelude + block],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("filename", ["program.md", "notes.txt"])
def test_non_python_input_never_enters_file_profiling(shell: tuple[Path, dict[str, str]], filename: str) -> None:
    """Keep existing documents mentioning an entry point as goal text.

    A Markdown or text file containing Python-like content must not be classified as a script to execute.
    """
    cwd, env = shell
    (cwd / filename).write_text('__main__\nraise RuntimeError("document executed")\n', encoding="utf-8")
    env["ARGUMENTS"] = filename

    result = _run(_block("plan", "NONFLAG_TOKEN_COUNT="), shell)

    assert result.returncode == 0, result.stderr
    assert "FILE_EXISTS=false" in result.stdout
    assert "OUT_ARG=\n" in result.stdout


def test_python_input_retains_output_override(shell: tuple[Path, dict[str, str]]) -> None:
    """Preserve the supported two-token Python input form.

    An existing script followed by a Markdown output path must keep both file classification and the explicit output.
    """
    cwd, env = shell
    (cwd / "train.py").write_text('if __name__ == "__main__":\n    pass\n', encoding="utf-8")
    env["ARGUMENTS"] = "train.py result.md"

    result = _run(_block("plan", "NONFLAG_TOKEN_COUNT="), shell)

    assert result.returncode == 0, result.stderr
    assert "FILE_EXISTS=true OUT_ARG=result.md" in result.stdout


def test_profile_block_rejects_document_even_with_stale_file_sentinel(
    shell: tuple[Path, dict[str, str]],
) -> None:
    """Prevent a stale file sentinel from bypassing the Python-file restriction.

    The profiling block receives an executable-looking document independently and must leave its side effect absent.
    """
    cwd, env = shell
    document = cwd / "program.md"
    document.write_text('if __name__ == "__main__":\n    open("EXECUTED", "w").close()\n', encoding="utf-8")
    (Path(env["TMPDIR"]) / "research-plan-file-arg-workflow-test").write_text("program.md\n", encoding="utf-8")

    result = _run(_block("plan", "python -m cProfile"), shell)

    assert result.returncode == 0, result.stderr
    assert "PROFILE_AVAILABLE=false" in result.stdout
    assert not (cwd / "EXECUTED").exists()


def test_profile_requires_explicit_choice_before_running_script() -> None:
    """Ask about script side effects after eligibility and before profiling.

    Runnable scripts require consent; the same question accepts a free-text goal without introducing a second stop.
    """
    lines = _lines("plan")
    profile = next(i for i, line in enumerate(lines) if "python -m cProfile -s" in line)
    consent = next(i for i, line in enumerate(lines) if line.startswith("**Profiling consent**"))
    precheck = next(i for i, line in enumerate(lines) if line.startswith("**Eligibility pre-check**"))

    assert precheck < consent < profile
    assert "PROFILE_ELIGIBLE=true" in lines[consent]
    assert "AskUserQuestion" in lines[consent]
    assert "checkpoints" in lines[consent]
    assert "downloads" in lines[consent]
    assert "Only" in lines[consent]
    assert "Profile" in lines[consent]
    assert "Provide goal" in lines[consent]
    assert "Abort" in lines[consent]
    assert "Other" in lines[consent]
    assert "without another question" in lines[consent]
    assert "Supply only **Profile** and **Abort** as predefined options" in lines[consent]


@pytest.mark.parametrize(
    ("filename", "content", "expected"),
    [
        pytest.param("train.py", 'if __name__ == "__main__": pass\n', "true", id="runnable-script"),
        pytest.param("lib.py", "VALUE = 1\n", "false", id="library-without-entry-point"),
        pytest.param("notes.md", "__main__\n", "false", id="document-with-entry-point-text"),
    ],
)
def test_profile_eligibility_is_checked_without_execution(
    shell: tuple[Path, dict[str, str]], filename: str, content: str, expected: str
) -> None:
    """Determine whether consent is needed without executing the supplied file.

    A library or document reaches goal entry without a profiling consent stop; a runnable Python script needs consent.
    """
    cwd, env = shell
    (cwd / filename).write_text(content + '\nopen("EXECUTED", "w").close()\n', encoding="utf-8")
    (Path(env["TMPDIR"]) / "research-plan-file-arg-workflow-test").write_text(filename + "\n", encoding="utf-8")

    result = _run(_block("plan", "PROFILE_ELIGIBLE=false"), shell)

    assert result.returncode == 0, result.stderr
    assert f"PROFILE_ELIGIBLE={expected}" in result.stdout
    assert not (cwd / "EXECUTED").exists()


def test_architect_gate_counts_expanded_unique_files() -> None:
    """Count expanded scope files before deciding whether architecture review applies.

    One broad glob can cover several files, while overlapping patterns must not inflate the count of unique paths.
    """
    gate = next(line for line in _lines("plan") if line.startswith("**Architect gate**"))

    assert "Glob" in gate
    assert "deduplicate" in gate
    assert "matched files" in gate
    assert "unmatched literal" in gate
    assert "agent_strategy = arch" in gate


def test_plan_uses_shared_foundry_probe() -> None:
    """Use the shared availability decision for source and marketplace installs.

    The plan gate must retain the canonical whole-plugin probe rather than testing for one advisor's role file.
    """
    gate = next(line for line in _lines("plan") if line.startswith("**Foundry availability check**"))
    shared = (_PLUGIN / "skills/_shared/agent-resolution.md").read_text(encoding="utf-8").splitlines()
    probe = next(line for line in shared if line.startswith("> **Foundry plugin check**"))
    canonical = probe.split("`", 2)[1]

    assert canonical in gate


def test_plan_agent_templates_have_action_names_and_descriptions() -> None:
    """Give advisor dispatches distinct task identities in the host UI.

    Scope and metric reviews must have unique names and short descriptions without depending on a common naming prefix.
    """
    spawns = [line for line in _lines("plan") if line.startswith("Agent(subagent_type=")]

    assert len(spawns) == 2
    names = [re.search(r'name="([^"]+)"', line).group(1) for line in spawns]
    descriptions = [re.search(r'description="([^"]+)"', line).group(1) for line in spawns]
    assert len(set(names)) == len(spawns)
    assert all(1 <= len(description.split()) <= 5 for description in descriptions)


def test_generated_config_contains_scalar_values_without_inline_comments() -> None:
    """Keep the emitted template compatible with the line-based judge and run parsers.

    Config values remain scalar and comment-free; metric direction must come from the wizard without a silent default.
    """
    lines = _lines("plan")
    start = lines.index("### Step P-P3: Write program.md")
    config = lines.index("## Config", start)
    end = lines.index("```", config + 2)
    values = dict(line.split(":", 1) for line in lines[config + 2 : end] if ":" in line)
    metric = lines.index("## Metric", start)

    assert "direction: <metric_direction from wizard: higher or lower>" in lines[metric:config]
    assert "colab_hw" not in values
    assert values["sandbox_network"].strip() == "none"
    assert values["metric_repeats"].strip() == "1"
    assert values["min_delta_pct"].strip() == "0.1"
    assert all("#" not in value and "|" not in value for value in values.values())


def test_kaggle_loads_deadlines_without_enabling_foundry_fallback() -> None:
    """Load the shared deadline protocol while retaining the required generation specialist.

    Kaggle keeps its accepted 900-second generation deadline and cannot gain a general-purpose fallback from the table.
    """
    lines = _lines("kaggle")
    before_conflict = lines[: next(i for i, line in enumerate(lines) if line.startswith("**Flag mutual"))]
    spawn = next(line for line in lines if line.startswith("**Spawn note**"))

    assert any("--emit agent-resolution.md" in line for line in before_conflict)
    assert "Agent waits" in spawn
    assert "900" in spawn
    assert any("no fallback" in line for line in before_conflict)


@pytest.mark.parametrize(
    ("listing_rows", "sample", "expected_output"),
    [
        pytest.param("SampleSubmission.csv,12\n", "SampleSubmission.csv", "id,target", id="mixed-case-sample"),
        pytest.param("sample_submission.csv,12\n", "sample_submission.csv", "id,target", id="standard-sample"),
        pytest.param("data/sample_submission.csv,12\n", "data/sample_submission.csv", "id,target", id="nested-sample"),
        pytest.param("train.csv,12\n", "", "no sample submission", id="no-sample"),
        pytest.param(
            "SampleSubmission.csv,12\nsample_submission.csv,13\n",
            "",
            "! AMBIGUOUS — submission format ungrounded",
            id="ambiguous-samples",
        ),
    ],
)
def test_kaggle_downloads_selected_sample_or_skips_absent_sample(
    shell: tuple[Path, dict[str, str]], listing_rows: str, sample: str, expected_output: str
) -> None:
    """Select the listed sample filename without inventing an absent or ambiguous download.

    Separate shells cover casing, nested names, no match, and multiple candidates with explicit oracles.
    """
    cwd, env = shell
    sentinels = Path(env["TMPDIR"])
    (sentinels / "kaggle-competition-name-workflow-test").write_text("example\n", encoding="utf-8")
    env["EXPECTED_SAMPLE"] = sample
    env["LISTING_ROWS"] = listing_rows
    stub = (
        "kaggle() {\n"
        'case "$2" in\n'
        'files) printf "name,size\\n%s" "$LISTING_ROWS";;\n'
        'leaderboard) printf "rank,score\\n1,1\\n";;\n'
        'download) printf "%s\\n" "$@" > download-args;\n'
        '  [ "$5" = "$EXPECTED_SAMPLE" ] || return 1;\n'
        '  printf "id,target\\n1,0\\n" > "$7/${5##*/}";;\n'
        "esac\n}\n"
    )

    listing = _run(_block("kaggle", "kaggle competitions files"), shell, stub)
    assert not (cwd / ".experiments").exists()
    result = _run(_block("kaggle", "kaggle competitions download"), shell, stub)

    assert listing.returncode == 0, listing.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert (cwd / "download-args").exists() == bool(sample)
    assert expected_output in result.stdout


def test_ambiguous_samples_print_all_candidates(shell: tuple[Path, dict[str, str]]) -> None:
    """Keep both candidate names visible when the listing cannot identify a single sample.

    Neither ambiguous file may be downloaded until the operator resolves the format from the displayed candidates.
    """
    cwd, env = shell
    (Path(env["TMPDIR"]) / "kaggle-competition-name-workflow-test").write_text("example\n", encoding="utf-8")
    listing = cwd / ".temp/kaggle/example-files.csv"
    listing.parent.mkdir(parents=True)
    listing.write_text("name,size\nSampleSubmission.csv,12\nsample_submission_alt.csv,13\n", encoding="utf-8")

    result = _run(_block("kaggle", "kaggle competitions download"), shell, "kaggle() { return 99; }\n")

    assert result.returncode == 0, result.stderr
    assert "SampleSubmission.csv" in result.stdout
    assert "sample_submission_alt.csv" in result.stdout
    assert "! AMBIGUOUS — submission format ungrounded" in result.stdout


def test_sample_override_resolves_ambiguity_without_trailing_newline(shell: tuple[Path, dict[str, str]]) -> None:
    """Resolve multiple candidates through the documented project-relative override.

    A Write-tool file without a final newline must still select its exact mixed-case filename.
    """
    cwd, env = shell
    (Path(env["TMPDIR"]) / "kaggle-competition-name-workflow-test").write_text("example\n", encoding="utf-8")
    listing = cwd / ".temp/kaggle/example-files.csv"
    listing.parent.mkdir(parents=True)
    listing.write_text("name,size\nSampleSubmission.csv,12\nsample_submission_alt.csv,13\n", encoding="utf-8")
    listing.with_name("example-sample-file.txt").write_text("SampleSubmission.csv", encoding="utf-8")

    result = _run(
        _block("kaggle", "kaggle competitions download"),
        shell,
        'kaggle() { printf "%s\\n" "$5" > selected-sample; printf "id,target\\n" > "$7/$5"; }\n',
    )

    assert result.returncode == 0, result.stderr
    assert "id,target" in result.stdout
    assert (cwd / "selected-sample").read_text(encoding="utf-8") == "SampleSubmission.csv\n"


@pytest.mark.parametrize("slug", ["my.comp", "https://example.test/competition"])
def test_sample_download_rejects_invalid_slug(shell: tuple[Path, dict[str, str]], slug: str) -> None:
    """Reject a dotted or URL slug before any sample download or data-directory creation.

    A stale slug sentinel cannot bypass the same bare-slug validation performed by the listing block.
    """
    cwd, env = shell
    sentinels = Path(env["TMPDIR"])
    (sentinels / "kaggle-competition-name-workflow-test").write_text("example\n", encoding="utf-8")
    (sentinels / "kaggle-slug-workflow-test").write_text(slug + "\n", encoding="utf-8")

    result = _run(_block("kaggle", "kaggle competitions download"), shell, "kaggle() { touch DOWNLOADED; }\n")

    assert result.returncode == 1, result.stderr
    assert "! BLOCKED — competition slug" in result.stdout
    assert not (cwd / "DOWNLOADED").exists()
    assert not (cwd / ".experiments").exists()


def test_sample_download_preserves_listing_slug_across_shells(shell: tuple[Path, dict[str, str]]) -> None:
    """Preserve the listing's competition slug when the notebook has a different local name.

    The download runs in a fresh shell without the override variable and must reload the persisted listing slug.
    """
    cwd, env = shell
    sentinels = Path(env["TMPDIR"])
    (sentinels / "kaggle-competition-name-workflow-test").write_text("notebook\n", encoding="utf-8")
    env["KAGGLE_SLUG"] = "actual-competition"
    listing = _run(
        _block("kaggle", "kaggle competitions files"),
        shell,
        'kaggle() { printf "name,size\\nSampleSubmission.csv,12\\n"; }\n',
    )
    env.pop("KAGGLE_SLUG")

    download = _run(
        _block("kaggle", "kaggle competitions download"),
        shell,
        'kaggle() { printf "%s\\n" "$3" > downloaded-slug; printf "id,target\\n" > "$7/$5"; }\n',
    )

    assert listing.returncode == 0, listing.stderr
    assert download.returncode == 0, download.stderr
    assert (cwd / "downloaded-slug").read_text(encoding="utf-8") == "actual-competition\n"


def test_sample_failure_keeps_cli_diagnostic_without_guessing_cause(shell: tuple[Path, dict[str, str]]) -> None:
    """Keep a failed selected-file download ungrounded without inventing an HTTP diagnosis.

    The CLI's error must survive alongside the blocked status rather than being replaced with a guessed rules error.
    """
    cwd, env = shell
    sentinels = Path(env["TMPDIR"])
    (sentinels / "kaggle-competition-name-workflow-test").write_text("example\n", encoding="utf-8")
    listing = cwd / ".temp/kaggle/example-files.csv"
    listing.parent.mkdir(parents=True)
    listing.write_text("name,size\nSampleSubmission.csv,12\n", encoding="utf-8")

    result = _run(
        _block("kaggle", "kaggle competitions download"),
        shell,
        'kaggle() { echo "download unavailable" >&2; return 1; }\n',
    )

    assert result.returncode == 1
    assert "download unavailable" in result.stderr
    assert "submission format as ungrounded" in result.stdout
    assert "malformed slug" not in result.stdout
    assert "rules not accepted" not in result.stdout


def test_sample_zip_is_extracted_before_reading_header(shell: tuple[Path, dict[str, str]]) -> None:
    """Extract a compressed single-file download before reading its CSV header.

    A ZIP-only payload must yield the expected file bytes and printed CSV header through the shipped extraction path.
    """
    cwd, env = shell
    sentinels = Path(env["TMPDIR"])
    (sentinels / "kaggle-competition-name-workflow-test").write_text("example\n", encoding="utf-8")
    listing = cwd / ".temp/kaggle/example-files.csv"
    listing.parent.mkdir(parents=True)
    listing.write_text("name,size\nSampleSubmission.csv,12\n", encoding="utf-8")
    data = cwd / ".experiments/kaggle/data/example"
    data.mkdir(parents=True)
    with zipfile.ZipFile(data / "SampleSubmission.csv.zip", "w") as archive:
        archive.writestr("SampleSubmission.csv", "id,target\n1,0\n")

    result = _run(_block("kaggle", "kaggle competitions download"), shell, "kaggle() { return 0; }\n")

    assert result.returncode == 0, result.stderr
    assert "id,target\n1,0" in result.stdout
    assert (data / "SampleSubmission.csv").read_bytes() == b"id,target\n1,0\n"


@pytest.mark.parametrize("inference", ["false", "true"])
def test_editor_reloads_output_and_prints_path_without_code_cli(
    shell: tuple[Path, dict[str, str]], inference: str
) -> None:
    """Print a usable notebook path when the editor CLI is absent.

    Fresh shells must reload the competition name and inference suffix before reporting the script's location.
    """
    _, env = shell
    sentinels = Path(env["TMPDIR"])
    (sentinels / "kaggle-competition-name-workflow-test").write_text("example\n", encoding="utf-8")
    (sentinels / "kaggle-inference-only-workflow-test").write_text(inference + "\n", encoding="utf-8")
    stub = 'command() { case "$*" in "-v code") return 1;; *) builtin command "$@";; esac; }\n'

    result = _run(_block("kaggle", "command -v code"), shell, stub)

    assert result.returncode == 0, result.stderr
    suffix = "-inference" if inference == "true" else ""
    assert f".experiments/kaggle/example{suffix}.py" in result.stdout


def test_editor_launch_receives_rehydrated_notebook_path(shell: tuple[Path, dict[str, str]]) -> None:
    """Pass the generated notebook path to an available editor in a fresh shell.

    An inference notebook's persisted mode must survive the shell boundary and appear in the editor's argument.
    """
    cwd, env = shell
    sentinels = Path(env["TMPDIR"])
    (sentinels / "kaggle-competition-name-workflow-test").write_text("example\n", encoding="utf-8")
    (sentinels / "kaggle-inference-only-workflow-test").write_text("true\n", encoding="utf-8")

    result = _run(_block("kaggle", "command -v code"), shell, 'code() { printf "%s\\n" "$@" > editor-args; }\n')

    assert result.returncode == 0, result.stderr
    assert (cwd / "editor-args").read_text(encoding="utf-8") == ".experiments/kaggle/example-inference.py\n"
