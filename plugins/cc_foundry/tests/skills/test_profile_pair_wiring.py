"""Hold foundry:profile's paired before/after mode to its shipped wiring.

The profile skill records one side of a before/after pair only when ``--pair-label`` and ``--pair-side`` arrive
together; Step 2 validates them and keeps them in the run's state, and Step 2c reads that state and records the side
after the cost section exists. A half-given pair must stop before any analyzer runs, or a run would be recorded under no
label or no side. Every profile fence must stay admitted to the blueprint manifest as a whole block, or the skill would
start prompting for commands that ran without a prompt before.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import build_blueprint_manifest as bbm
import pytest
from _hook_env import _bash_runs_posix_script

SKILL = Path(__file__).resolve().parents[2] / "skills" / "profile" / "SKILL.md"


def _lines() -> list[str]:
    """Return the profile skill's lines."""
    return SKILL.read_text(encoding="utf-8").splitlines()


def _line_with(fragment: str) -> str:
    """Return the single skill line containing ``fragment``."""
    matches = [line for line in _lines() if fragment in line]
    assert len(matches) == 1, matches
    return matches[0]


def _report_setup() -> str:
    """Extract the setup portion of the first analyzer recipe."""
    lines = _lines()
    start = lines.index("```bash") + 1
    end = lines.index('set --; [ -n "$SESSION_ID" ] && set -- --session-id "$SESSION_ID"', start)
    return "\n".join(lines[start:end])


def _run_setup(tmp_path: Path, session: str, arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the shipped parser, state writer and directory allocation at a fixed timestamp."""
    script = (
        'export TMPDIR="$1" CLAUDE_PLUGIN_ROOT="$2" CLAUDE_CODE_SESSION_ID="$3"\n'
        'ARGUMENTS="$4"; PROFILE_PYTHON="$5"\n'
        'python() { "$PROFILE_PYTHON" "$@"; }\n'
        'date() { printf "2026-01-01T00-00-00Z\\n"; }\n' + _report_setup()
    )
    return subprocess.run(
        ["bash", "-c", script, "_", str(tmp_path), str(SKILL.parents[2]), session, arguments, sys.executable],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )


def test_step_two_parser_declares_both_pair_flags() -> None:
    """The flag parser receives ``pair-label`` and ``pair-side`` as value flags, so their values reach the state."""
    tokens = shlex.split(_line_with('/bin/parse-skill-flags.py"'), comments=True)
    value_flags = tokens[tokens.index("--value-flags") + 1].split(",")
    assert {"pair-label", "pair-side"} <= set(value_flags)


def test_step_one_consumes_values_before_checking_flag_names() -> None:
    """Flag-shaped values belong to their option and are forwarded with their original spelling."""
    instruction = _line_with("**Unsupported flag check**")
    assert "left to right" in instruction
    assert "consume the value before checking the next option" in instruction
    assert "`--name=VALUE`" in instruction


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="no POSIX bash on PATH")
def test_report_directories_are_unique_at_the_same_timestamp(tmp_path: Path) -> None:
    """Two invocations in one clock tick own two independently created report directories."""
    before = _run_setup(tmp_path, "before", "")
    after = _run_setup(tmp_path, "after", "")
    assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
    assert before.stdout.splitlines()[0] != after.stdout.splitlines()[0]
    assert len(list((tmp_path / ".reports" / "profile").iterdir())) == 2


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="no POSIX bash on PATH")
def test_setup_stops_when_it_cannot_allocate_a_report_directory(tmp_path: Path) -> None:
    """An unavailable report root prevents publication of the run pointer."""
    report_root = tmp_path / ".reports" / "profile"
    report_root.parent.mkdir()
    report_root.touch()
    setup = _run_setup(tmp_path, "blocked", "")
    assert setup.returncode != 0
    assert not (tmp_path / "foundry-profile-state-blocked").exists()


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="no POSIX bash on PATH")
@pytest.mark.parametrize("label", ["baseline\n", "baseline\nother"])
def test_setup_refuses_to_truncate_a_multiline_label(tmp_path: Path, label: str) -> None:
    """A quoted label with line breaks is refused as a whole rather than recorded under its first line."""
    setup = _run_setup(tmp_path, "invalid", f'--pair-label="{label}" --pair-side=before')
    assert setup.returncode != 0
    assert not (tmp_path / "foundry-profile-state-invalid").exists()


def test_step_two_c_records_after_the_cost_step() -> None:
    """Step 2c follows Step 2b, so a run's cost total exists before its side is recorded."""
    headings = [line for line in _lines() if line.startswith("## Step 2")]
    assert headings.index("## Step 2c: Record a paired side (only with `--pair-label`)") > next(
        index for index, heading in enumerate(headings) if heading.startswith("## Step 2b")
    )


def test_step_two_c_appends_the_comparison_to_the_report() -> None:
    """The recording command appends the paired section to the run's own report."""
    tokens = shlex.split(_line_with('/bin/profile_pair.py" record'), comments=True)
    assert tokens[tokens.index("--append-to") + 1] == "$REPORT_DIR/report.md"


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="no POSIX bash on PATH")
@pytest.mark.parametrize("label", ["/oss:resolve", "-baseline", "-", "--", "--x", "--noop", "--keep", "--since"])
def test_record_command_preserves_valid_label(tmp_path: Path, label: str) -> None:
    """The shared parser, state and shipped record command preserve punctuation and flag-shaped values."""
    setup = _run_setup(tmp_path, "label-test", f"--pair-label={label} --pair-side=before")
    assert setup.returncode == 0, setup.stderr
    report_dir = tmp_path / setup.stdout.splitlines()[0].removeprefix("REPORT_DIR=")
    report = report_dir / "report.md"
    report.write_text(
        "Scope:      window=1h, sessions=1, total_wall=00:01:00\n- AskUserQuestion idle (excluded): 00:00:00\n",
        encoding="utf-8",
    )
    (report_dir / "result.jsonl").write_text("{}\n", encoding="utf-8")
    script = (
        'PLUGIN_ROOT="$1"; TMPDIR="$2"; CSID=label-test; PROFILE_PYTHON="$3"\n'
        'python() { "$PROFILE_PYTHON" "$@"; }\n'
        + _line_with("REPORT_DIR) REPORT_DIR=$V;; PAIR_LABEL)")
        + "\n"
        + _line_with('/bin/profile_pair.py" record')
    )
    run = subprocess.run(
        ["bash", "-c", script, "_", str(SKILL.parents[2]), str(tmp_path), sys.executable],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert f"Label: `{label}`" in report.read_text(encoding="utf-8")


def test_every_profile_fence_is_admitted_as_a_block() -> None:
    """Each bash fence of the skill gets a whole-block manifest entry, so none of them prompts."""
    text = SKILL.read_text(encoding="utf-8")
    fences = [block for block in bbm.parse_blocks(text, str(SKILL)) if block.lang_detected == "bash"]
    kinds = [
        {
            record["kind"]
            for record in bbm.block_entries(bbm.normalize(block.content), "f.md:1", plugin_name="foundry")[0].values()
        }
        for block in fences
    ]
    assert len(fences) == 3
    assert all("block" in found for found in kinds), kinds


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="no POSIX bash on PATH")
@pytest.mark.parametrize(
    ("label", "side", "allowed"),
    [
        pytest.param("", "", True, id="no-pair"),
        pytest.param("/oss:resolve", "before", True, id="complete-pair"),
        pytest.param("/oss:resolve", "", False, id="label-without-side"),
        pytest.param("", "after", False, id="side-without-label"),
        pytest.param("two words", "after", False, id="label-with-space"),
        pytest.param("/oss:resolve", "during", False, id="unknown-side"),
    ],
)
def test_step_two_refuses_a_half_given_pair(label: str, side: str, allowed: bool) -> None:
    """The shipped validation lines let a pair through only when label and side are both valid, or both absent."""
    checks = "\n".join([_line_with("invalid --pair-label"), _line_with("go together")])
    script = f'PAIR_LABEL="$1"; PAIR_SIDE="$2"\n{checks}'
    result = subprocess.run(["bash", "-c", script, "_", label, side], capture_output=True, timeout=15, check=False)
    assert (result.returncode == 0) is allowed
