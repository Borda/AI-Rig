"""Exercise calibration bridge probes without relying on a persistent tool shell."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script
from _plugin_payload import install_foundry

_CALIBRATE = Path(__file__).resolve().parents[2] / "skills/calibrate"


def _probe_block() -> str:
    """Read the bridge preflight recipe from its Markdown fence."""
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line == "### Pre-flight — Codex availability")
    first = next(index for index in range(start + 1, len(lines)) if lines[index] == "```bash")
    last = next(index for index in range(first + 1, len(lines)) if lines[index] == "```")
    return "\n".join(lines[first + 1 : last])


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
@pytest.mark.parametrize(
    ("helper_result", "status", "available"),
    [
        pytest.param("printf 'available\\npartial'; return 9", "absent", "false", id="partial-failure"),
        pytest.param("printf 'available\\n'", "available", "true", id="available"),
        pytest.param("printf 'disabled\\n'", "disabled", "false", id="disabled"),
    ],
)
def test_bridge_preflight_persists_and_exposes_a_single_status(
    tmp_path: Path, helper_result: str, status: str, available: str
) -> None:
    """Failed probes replace partial output, and successful probes expose reusable status."""
    script = _probe_block().replace("<TIMESTAMP>", "run").replace("<RUN_SLUG>", "example-inspect")
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'python() {{ case "$1" in */check_bridge.py) {helper_result};; *) "$TEST_PYTHON" "$@";; esac; }}\n'
            + script,
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "CLAUDE_PLUGIN_ROOT": str(install_foundry(tmp_path)),
            "TEST_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".reports/calibrate/run/example-inspect/bridge-status").read_text() == status + "\n"
    assert f"CODEX_AVAILABLE={available}" in result.stdout.splitlines()


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
def test_local_pipeline_uses_the_scope_resolved_manifest_layout(tmp_path: Path) -> None:
    """The selected skill file survives dispatch when its manifest uses a custom directory."""
    source = tmp_path / "plugins/cc_example/procedures/inspect/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("custom skill\n", encoding="utf-8")
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line == "### Pre-flight — local file resolution")
    first = next(index for index in range(start + 1, len(lines)) if lines[index] == "```bash")
    last = next(index for index in range(first + 1, len(lines)) if lines[index] == "```")
    script = "\n".join(lines[first + 1 : last])
    script = script.replace("<LOCAL_MODE>", "true").replace("<TARGET>", "/example:inspect")
    script = script.replace("<TARGET_SOURCE_PATH>", "plugins/cc_example/procedures/inspect/SKILL.md")
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "local mode: using plugins/cc_example/procedures/inspect/SKILL.md"


def _quota_block() -> str:
    """Read the quota-stop recording recipe: the Codex availability section's fence that writes the stop."""
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line == "### Pre-flight — Codex availability")
    end = next(index for index in range(start + 1, len(lines)) if lines[index].startswith("### "))
    opens = [index for index in range(start, end) if lines[index] == "```bash"]
    bodies = ["\n".join(lines[index + 1 : lines.index("```", index + 1)]) for index in opens]
    return next(body for body in bodies if '> "${RUN_DIR%/*}/codex-blocked.txt"' in body)


def _status_reread() -> str:
    """Return the pre-flight lines that derive availability from the persisted status, as later calls re-read it."""
    needles = ("IFS= read -r CODEX_STATUS", 'if [ "$CODEX_STATUS" = "available" ]', "printf 'CODEX_AVAILABLE=")
    lines = _probe_block().splitlines()
    return "\n".join(next(line for line in lines if needle in line) for needle in needles)


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
def test_quota_stop_makes_every_later_status_read_unavailable(tmp_path: Path) -> None:
    """A recorded quota stop replaces the persisted status, so the remaining phases run Claude-only.

    The pipeline never re-probes midway and re-reads ``bridge-status`` before each later Codex call, so overwriting that
    file is what keeps Phase 3b from spending one doomed call per problem after the peer ran out of credits.
    """
    run_dir = tmp_path / ".reports/calibrate/run/example-inspect"
    run_dir.mkdir(parents=True)
    (run_dir / "bridge-status").write_text("available\n", encoding="utf-8")
    script = (
        (_quota_block() + "\n" + _status_reread())
        .replace("<TIMESTAMP>", "run")
        .replace("<RUN_SLUG>", "example-inspect")
    )
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=dict(os.environ), capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert (run_dir / "bridge-status").read_text(encoding="utf-8") == "codex-blocked:quota\n"
    assert (run_dir.parent / "codex-blocked.txt").read_text(encoding="utf-8") == "codex-blocked:quota\n"
    assert "CODEX_AVAILABLE=false" in result.stdout.splitlines()


def _quota_rule() -> list[str]:
    """Return the **Quota stop** rule lines: its lead sentence and bullets, up to the recording fence."""
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("**Quota stop**"))
    end = lines.index("```bash", start)
    return lines[start:end]


def test_quota_stop_decides_only_from_the_harness_incident() -> None:
    """Only the incident file's quota fault stops Codex; other faults and verdict text keep today's path."""
    rule = _quota_rule()
    assert "`fault`" in rule[0]
    assert any("`quota-exhausted`" in line and "run the block below at once" in line for line in rule)
    assert any("null `incident`" in line and "`rate-limited`" in line and "`capacity`" in line for line in rule)
    assert any("never decide from verdict or blockers text" in line for line in rule)


def test_scoring_never_retries_a_quota_fault() -> None:
    """Phase 3b's retry-once rule excludes a quota fault, which no retry inside the run can clear."""
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    retry = next(line for line in lines if line.startswith("- Quota fault:"))
    assert "`quota-exhausted`" in retry
    assert "never retried" in retry
    assert "every remaining problem" in retry


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
@pytest.mark.parametrize(
    ("run_marker", "status", "available"),
    [
        pytest.param("codex-blocked:quota\n", "codex-blocked:quota", "false", id="sibling-hit-quota"),
        pytest.param("", "available", "true", id="no-run-stop"),
    ],
)
def test_preflight_inherits_a_sibling_pipelines_quota_stop(
    tmp_path: Path, run_marker: str, status: str, available: str
) -> None:
    """A pipeline starting after a sibling's quota stop treats Codex as unavailable for the whole run.

    Every pipeline of one calibration run shares the ``<TIMESTAMP>`` directory. The probe still reports the bridge as
    installed and enabled, so only the run-level marker can keep the new pipeline from paying its own doomed Codex call;
    an empty marker keeps today's availability.
    """
    run_root = tmp_path / ".reports/calibrate/run"
    run_root.mkdir(parents=True)
    (run_root / "codex-blocked.txt").write_text(run_marker, encoding="utf-8")
    script = _probe_block().replace("<TIMESTAMP>", "run").replace("<RUN_SLUG>", "example-inspect")
    result = subprocess.run(
        [
            "bash",
            "-c",
            'python() { case "$1" in */check_bridge.py) printf \'available\\n\';; *) "$TEST_PYTHON" "$@";; esac; }\n'
            + script,
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "CLAUDE_PLUGIN_ROOT": str(install_foundry(tmp_path)),
            "TEST_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (run_root / "example-inspect/bridge-status").read_text(encoding="utf-8") == status + "\n"
    assert f"CODEX_AVAILABLE={available}" in result.stdout.splitlines()


def test_later_status_reads_honour_the_run_level_stop() -> None:
    """In-flight pipelines re-read the run marker beside their own status, so a sibling's stop reaches them too."""
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    reread = next(line for line in lines if line.startswith("- Run-level stop:"))
    assert "<TIMESTAMP>/codex-blocked.txt" in reread
    limit = next(line for line in _quota_rule() if "A pipeline already waiting on a Codex call" in line)
    assert "at most one" in limit


def test_source_mode_follows_the_generated_problems_not_the_live_status() -> None:
    """Codex problems generated before a scoring-time quota stop keep the run labelled dual-source.

    Phase 3b's quota stop rewrites ``bridge-status``; deriving ``source_mode`` from that live value would label a run
    claude-only while ``codex_problems_pct`` and ``recall_codex_problems`` still count Codex problems.
    """
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    rule = next(line for line in lines if line.startswith("- `source_mode` ="))
    assert "CODEX_AVAILABLE" not in rule
    assert '`source: "codex"`' in rule
    assert "never" in rule
    assert "bridge-status" in rule
    assert any("Source: dual" in line and "Scorer: single" in line and "codex-blocked:quota" in line for line in lines)


def test_challenger_targets_skip_codex_after_a_quota_stop() -> None:
    """Benchmarking the challenger after a quota stop does not spend one doomed Codex call per problem."""
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    assert any(
        "`foundry:challenger`" in line and "--no-codex" in line and "codex-blocked:quota" in line for line in lines
    )


_CHALLENGER_QUOTA_LINE = (
    "⚠ codex-blocked:quota — Codex out of credits (incident fault `quota-exhausted`); cross-check not retried."
    " Report above is Claude-only."
)


def _challenger_signal_block() -> str:
    """Read the recipe that turns a challenger response's quota line into the run's quota stop."""
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if "a response whose `## Codex Cross-Check`" in line)
    first = next(index for index in range(start + 1, len(lines)) if lines[index] == "```bash")
    last = next(index for index in range(first + 1, len(lines)) if lines[index] == "```")
    return "\n".join(lines[first + 1 : last])


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
@pytest.mark.parametrize(
    ("responses", "recorded"),
    [
        pytest.param(
            {"p1": f"## Challenge: x\n\n## Codex Cross-Check\n\n{_CHALLENGER_QUOTA_LINE}\n\n## Confidence\n"},
            True,
            id="quota-line-opens-section",
        ),
        pytest.param(
            {
                "p1": "## Codex Cross-Check\n\nNo convergent findings.\n",
                "p2": f"## Codex Cross-Check\n{_CHALLENGER_QUOTA_LINE}\n",
            },
            True,
            id="one-of-several-responses",
        ),
        pytest.param(
            {"p1": f"1. quoted: {_CHALLENGER_QUOTA_LINE}\n\n## Codex Cross-Check\n\nCodex found two issues.\n"},
            False,
            id="token-quoted-in-a-finding",
        ),
        pytest.param(
            {"p1": f"## Codex Cross-Check\n\nCodex found two issues.\n{_CHALLENGER_QUOTA_LINE}\n"},
            False,
            id="token-later-in-the-section",
        ),
        pytest.param(
            {"p1": "## Codex Cross-Check\n\n⚠ **Codex cross-check failed** — model at capacity\n"},
            False,
            id="other-failure",
        ),
        pytest.param({}, False, id="no-responses-yet"),
        pytest.param(
            {"p1": f"## Codex Cross-Check\n\nCodex cross-check completed.\n\n{_CHALLENGER_QUOTA_LINE}\n"},
            False,
            id="codex-output-opens-with-the-quota-line",
        ),
        pytest.param(
            {
                "p1": "## Codex Cross-Check\n\nCodex cross-check completed.\n\n"
                f"## Codex Cross-Check\n\n{_CHALLENGER_QUOTA_LINE}\n"
            },
            False,
            id="codex-output-repeats-the-heading",
        ),
        pytest.param(
            {
                "p1": f"```markdown\n## Codex Cross-Check\n\n{_CHALLENGER_QUOTA_LINE}\n```\n\n"
                "## Codex Cross-Check\n\nCodex cross-check skipped (`--no-codex`).\n"
            },
            False,
            id="backtick-fenced-quote",
        ),
        pytest.param(
            {
                "p1": f"~~~\n## Codex Cross-Check\n{_CHALLENGER_QUOTA_LINE}\n~~~\n"
                "## Codex Cross-Check\n\nCodex cross-check skipped (`--no-codex`).\n"
            },
            False,
            id="tilde-fenced-quote",
        ),
        pytest.param(
            {
                "p1": f"````markdown\n```\n## Codex Cross-Check\n\n{_CHALLENGER_QUOTA_LINE}\n```\n````\n\n"
                "## Codex Cross-Check\n\nCodex cross-check completed.\n"
            },
            False,
            id="longer-fence-wraps-a-shorter-one",
        ),
        pytest.param(
            {"p1": f"## Codex Cross-Check\n\n```\n{_CHALLENGER_QUOTA_LINE}\n```\n"},
            False,
            id="fence-opens-the-section",
        ),
        pytest.param(
            {"p1": f"```text\nexample\n```\n\n## Codex Cross-Check\n\n{_CHALLENGER_QUOTA_LINE}\n"},
            True,
            id="closed-fence-before-a-real-quota-section",
        ),
        pytest.param(
            {"p1": f"## Challenge: x\r\n\r\n## Codex Cross-Check\r\n\r\n{_CHALLENGER_QUOTA_LINE}\r\n"},
            True,
            id="crlf-line-endings",
        ),
        pytest.param(
            {
                "p1": f"## Codex Cross-Check notes\n\n{_CHALLENGER_QUOTA_LINE}\n\n"
                "## Codex Cross-Check\n\nCodex cross-check completed.\n"
            },
            False,
            id="longer-heading-is-another-section",
        ),
    ],
)
def test_challenger_quota_line_records_the_run_quota_stop(
    tmp_path: Path, responses: dict[str, str], recorded: bool
) -> None:
    """A challenger response whose Cross-Check section opens with the quota line records the run-wide stop.

    Calibrating ``foundry:challenger`` spawns one Opus child per problem, each running its own Codex cross-check; the
    reply is a fixed ``Wrote:`` line that rarely arrives, so the response file is the only carrier. Only the first
    non-blank line under the first Cross-Check heading outside a code fence counts: a finding quoting the token, a
    fenced quote of the template, or Codex output repeating the line or the heading below the challenger's own opening
    line must not stop Codex. Responses are written byte-exact, so line endings are the case's own on every host.
    """
    run_dir = tmp_path / ".reports/calibrate/run/challenger-inspect"
    run_dir.mkdir(parents=True)
    (run_dir / "bridge-status").write_text("available\n", encoding="utf-8")
    for problem_id, text in responses.items():
        (run_dir / f"response-{problem_id}.md").write_text(text, encoding="utf-8", newline="\n")
    script = _challenger_signal_block().replace("<TIMESTAMP>", "run").replace("<RUN_SLUG>", "challenger-inspect")
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=dict(os.environ), capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    expected = "codex-blocked:quota\n" if recorded else "available\n"
    assert (run_dir / "bridge-status").read_text(encoding="utf-8") == expected
    assert (run_dir.parent / "codex-blocked.txt").exists() is recorded


def test_challenger_quota_line_check_runs_at_every_wake_up() -> None:
    """The response check runs at every wake-up of a challenger phase, so the next problem already skips Codex."""
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    lead = next(index for index, line in enumerate(lines) if "a response whose `## Codex Cross-Check`" in line)
    fence = lines.index("```bash", lead)
    assert "`⚠ codex-blocked:quota`" in lines[lead]
    assert any("every wake-up" in line and "before the next dispatch" in line for line in lines[lead:fence])


def test_challenger_quota_line_rule_names_the_first_unfenced_heading() -> None:
    """The prose beside the recipe states which line counts, matching what the recipe reads.

    A model following the prose instead of running the block must reach the same answer: only the first heading
    outside a code fence, and only its first non-blank line.
    """
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    lead = next(index for index, line in enumerate(lines) if "a response whose `## Codex Cross-Check`" in line)
    fence = lines.index("```bash", lead)
    rule = next(line for line in lines[lead:fence] if line.startswith("- Only"))
    assert "first non-blank line under the first `## Codex Cross-Check` heading outside any code fence" in rule
