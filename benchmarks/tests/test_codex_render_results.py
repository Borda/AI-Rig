"""Terminal rendering tests for the Codex structural runner's ``--render-results`` mode."""

from __future__ import annotations

import importlib.util
import io
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCHMARKS_DIR))

from _bench_codex import runtime as codex_runtime  # noqa: E402
from _bench_common.presentation import (  # noqa: E402
    BENCHMARK_OUTPUT_WIDTH,
    LEGEND_CLOSE_RULE,
    LEGEND_OPEN_RULE,
)

SCRIPT_PATH = BENCHMARKS_DIR / "run-codex-structural.py"
#: Select-graphic-rendition escapes, stripped to measure a styled row's visible width.
_ANSI_CODE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(name="script_run_codex", scope="module")
def _script_run_codex() -> Any:
    """Load the Codex adapter without executing its command-line entry point.

    Example:
        >>> getfixture("script_run_codex").__name__
        'run_codemap_codex'
    """
    spec = importlib.util.spec_from_file_location("run_codemap_codex", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Codex adapter at {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _structural_probe_row(arm: str) -> str:
    """Build the probe row the structural runner emits for one arm."""
    return codex_runtime.format_probe_row(
        arm,
        {"codemap": arm != "A_plain", "use": codex_runtime.probe_use(arm), "codemap_python": "absent"},
    )


def _render_result_stream(input_text: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the runner's stream-rendering mode with captured text output."""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--render-results", *args],
        cwd=BENCHMARKS_DIR.parent,
        input=input_text,
        capture_output=True,
        encoding="utf-8",
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("arm", "color_code"),
    [
        pytest.param("A_plain", "33", id="plain-yellow"),
        pytest.param("B_auto", "36", id="direct-cyan"),
        pytest.param("C_strict", "35", id="skill-magenta"),
        pytest.param("B_auto", "36", id="agentic-auto-cyan"),
        pytest.param("C_strict", "35", id="agentic-required-magenta"),
    ],
)
def test_render_results_force_color_maps_each_arm_to_its_review_color(arm: str, color_code: str) -> None:
    """The test-only flag proves the exact A/B/C terminal palette."""
    row = f"(1/3) ✓  FN-02  rep=1  {arm}  quality=1.000\n"

    completed = _render_result_stream(row, "--force-color")

    assert completed.returncode == 0, completed.stderr
    assert f"\x1b[{color_code}m" in completed.stdout
    assert row.rstrip("\n") in completed.stdout
    assert completed.stdout.endswith("\x1b[0m\n")


def test_render_results_recovers_bare_force_color_flag_at_cli_boundary(
    script_run_codex: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test-only bare flag survives a Windows Fire subprocess boundary."""
    received: list[bool] = []

    def _render(_rows: object, _output: object, *, force_color: bool, hide_plan: bool) -> None:
        """Record renderer flags without consuming pytest's standard streams."""
        assert hide_plan is False
        received.append(force_color)

    monkeypatch.setattr(codex_runtime, "render_result_rows", _render)
    monkeypatch.setattr(script_run_codex.sys, "argv", ["runner", "--render-results", "--force-color"])

    script_run_codex.cli(render_results=True)

    assert received == [True]


def test_render_results_force_color_renders_legend_as_bounded_rich_panel() -> None:
    """Interactive rendering turns the plain legend block into one titled Rich box."""
    input_text = (
        f"{LEGEND_OPEN_RULE}\n"
        "  treatments: A_plain=no Codemap\n"
        "  status: ✓ completed, ✗ failed\n"
        f"{LEGEND_CLOSE_RULE}\n"
        "(1/3) ✓  FN-02  rep=1  A_plain  quality=1.000\n"
    )

    completed = _render_result_stream(input_text, "--force-color")

    assert completed.returncode == 0, completed.stderr
    assert "Legend" in completed.stdout
    assert "End legend" in completed.stdout
    assert "treatments: A_plain=no Codemap" in completed.stdout
    assert completed.stdout.count("Legend") == 1
    assert completed.stdout.count("End legend") == 1


def test_render_results_preserves_noninteractive_stream_byte_for_byte() -> None:
    """Redirected renderer output remains a plain machine-reviewable stream."""
    input_text = "INFO keep this byte-for-byte\n(1/3) ✓  FN-02  rep=1  A_plain  quality=1.000\n"

    completed = _render_result_stream(input_text)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == input_text
    assert "\x1b[" not in completed.stdout


@pytest.mark.parametrize("terminal", [False, True], ids=["redirected", "terminal"])
@pytest.mark.parametrize("force_color", [False, True], ids=["automatic-color", "forced-color"])
def test_render_results_preserves_long_rows_without_inserted_wraps(
    terminal: bool, force_color: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Long result rows retain all fields on one logical line in every output mode."""
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: terminal)
    row = (
        "(37/48) ✓  BA-13  rep=1  A_plain     in=180.4k  out=  3.9k  time=2m46s  "
        "SCORE=0.889  EREC=1.000  RREC=1.000  DEFF=0.500  answer:✓  treatment:✓  codemap-used:✗\n"
    )
    assert len(row.rstrip("\n")) > BENCHMARK_OUTPUT_WIDTH

    codex_runtime.render_result_rows([row], stream, force_color=force_color)

    assert _ANSI_CODE.sub("", stream.getvalue()) == row
    assert ("\x1b[" in stream.getvalue()) == (terminal or force_color)


def test_render_results_noninteractive_legend_is_byte_stable() -> None:
    """The noninteractive renderer does not rewrite a bounded plain legend."""
    input_text = (
        f"{LEGEND_OPEN_RULE}\n"
        "  treatments: A_plain=no Codemap\n"
        "  status: ✓ completed, ✗ failed\n"
        f"{LEGEND_CLOSE_RULE}\n"
        "(1/3) ✓  FN-02  rep=1  A_plain  quality=1.000\n"
    )

    completed = _render_result_stream(input_text)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == input_text


def test_render_results_force_color_preserves_unknown_and_non_result_rows() -> None:
    """Only recognized A/B/C progress rows receive terminal styling."""
    input_text = "INFO preparation\n(1/3) ✓  FN-02  rep=1  unknown  quality=1.000\n"

    completed = _render_result_stream(input_text, "--force-color")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == input_text
    assert "\x1b[" not in completed.stdout


def test_render_results_hide_plan_omits_only_human_plan_rows() -> None:
    """The optional renderer filter removes PLAN rows without changing other output."""
    input_text = (
        "LEGEND  fields\n"
        "PROBE   A_plain   codemap=false\n"
        "PLAN    SE-01  rep=1  A_plain\n"
        "PLAN    FN-02  rep=1  B_auto\n"
        "CONTROL\tcell_wall_clock_seconds=600\n"
        "ARTIFACTS  telemetry=run.jsonl  metadata=metadata.json\n"
        "(1/3) ✓  SE-01  rep=1  A_plain  quality=1.000\n"
        "SUMMARY\tstatus=completed\n"
    )
    expected = (
        "LEGEND  fields\n"
        "PROBE   A_plain   codemap=false\n"
        "CONTROL\tcell_wall_clock_seconds=600\n"
        "ARTIFACTS  telemetry=run.jsonl  metadata=metadata.json\n"
        "(1/3) ✓  SE-01  rep=1  A_plain  quality=1.000\n"
        "SUMMARY\tstatus=completed\n"
    )

    completed = _render_result_stream(input_text, "--hide-plan")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == expected


def test_hide_plan_requires_render_results_mode() -> None:
    """The internal stream filter cannot alter normal benchmark execution."""
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--hide-plan"],
        cwd=BENCHMARKS_DIR.parent,
        input="",
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "--hide-plan requires --render-results" in completed.stderr


def test_replayed_legend_panel_matches_the_shared_benchmark_width() -> None:
    """Archived legends replay at 120 columns regardless of the detected terminal width."""
    stream = io.StringIO()

    codex_runtime.render_result_rows(
        f"{LEGEND_OPEN_RULE}\n  status: done\n{LEGEND_CLOSE_RULE}\n".splitlines(keepends=True),
        stream,
        force_color=True,
    )

    visible = [_ANSI_CODE.sub("", line) for line in stream.getvalue().splitlines()]
    assert [line[0] for line in visible] == ["╭", "│", "╰"]
    assert {len(line) for line in visible} == {120}
    assert BENCHMARK_OUTPUT_WIDTH == 120


def test_structural_legend_keeps_its_plain_framed_form_when_redirected(capsys: pytest.CaptureFixture[str]) -> None:
    """A redirected structural legend still writes the exact framed text its run logs archive.

    The legend moved onto the shared panel helper for terminals. Run logs are byte-compared and replayed long after the
    run, so the non-terminal branch has to stay identical to the constant the runner also writes into the log itself.
    """
    codex_runtime.print_structural_legend()

    printed = capsys.readouterr().out

    assert printed == f"{codex_runtime.STRUCTURAL_OUTPUT_LEGEND}\n"
    assert printed.splitlines()[0] == LEGEND_OPEN_RULE
    assert printed.splitlines()[1:-1] == list(codex_runtime.STRUCTURAL_LEGEND_BODY)
    assert printed.splitlines()[-1] == LEGEND_CLOSE_RULE


def test_section_rule_keeps_plain_equals_marks_when_redirected(capsys: pytest.CaptureFixture[str]) -> None:
    """A redirected section banner keeps the ``== title ==`` form the run logs already carry.

    Stage banners became Rich rules on a terminal. Their redirected form is what an archived log and the launcher's own
    banners look like, so it has to survive the change unaltered.
    """
    codex_runtime.print_section_rule("STAGE 1/4: structural (1 tasks, 3 cells)")

    assert capsys.readouterr().out == "== STAGE 1/4: structural (1 tasks, 3 cells) ==\n"


def test_probe_rows_align_across_arms_without_tabs() -> None:
    """Probe rows put every field in the same column for every arm, with no tab characters.

    Tab-separated probe rows advanced to the next tab stop, so the longer ``C_strict`` label pushed its own fields one
    stop right and no column lined up with the row above it.
    """
    rows = [_structural_probe_row(arm) for arm in ("A_plain", "B_auto", "C_strict")]

    assert not any("\t" in row for row in rows)
    assert {row.index("codemap_python=") for row in rows} == {len(rows[0]) - len("codemap_python=absent")}


def test_optional_and_required_arms_read_differently_at_a_glance() -> None:
    """A structural probe row tells the optional arm apart from the required one.

    Scenario: B_auto and C_strict both find the Codemap binary, so a row carrying only the measured
    ``codemap=true`` said nothing about the difference between an arm that may call Codemap and one
    the Skill obliges to. The measured field is unchanged, so a binary missing under either arm
    still reads as ``codemap=false`` rather than being papered over by the contract word.
    """
    optional, required = (_structural_probe_row(arm) for arm in ("B_auto", "C_strict"))

    assert optional != required
    assert "codemap=true   use=optional" in optional
    assert "codemap=true   use=required" in required


@pytest.mark.parametrize(
    ("show_paid_command", "paid_block_expected"),
    [
        pytest.param(True, True, id="default-prints-paid-command"),
        pytest.param(False, False, id="suppressed-by-no-paid-command"),
    ],
)
def test_paid_command_suppression_leaves_scope_and_probes_intact(
    script_run_codex: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    show_paid_command: bool,
    paid_block_expected: bool,
) -> None:
    """Suppressing the paid command removes only that block, never SCOPE or DESIGN.

    Scenario: the launcher runs a one-task environment probe during every full study, and that probe
    printed a complete copyable command for a 9-cell study nobody asked for, right before the real
    one. The launcher parses SCOPE out of the same dry run, so suppression must not touch it.
    """
    import _bench_codex.stage_fix as fix_stage
    import _bench_codex.stage_readcrop as readcrop_stage

    stages = [{"stage_id": "structural", "task_ids": ["FN-02"], "total_cells": 3, "repetitions": 1}]
    stages[0]["scope_sha256"] = "1" * 64
    selection = {
        "selection_mode": "explicit",
        "selectors": ["FN-02"],
        "task_ids": ["FN-02"],
        "total_tasks": 1,
        "total_cells": 3,
        "stages": stages,
    }
    scope = {**selection, "scope_sha256": "1234567890123456" + "a" * 48}
    monkeypatch.setattr(script_run_codex, "resolve_task_selection", lambda *_args: selection)
    monkeypatch.setattr(script_run_codex, "_resolve_execution_scope", lambda **_kwargs: scope)
    monkeypatch.setattr(
        script_run_codex, "main", lambda **_kwargs: print("PROBE   A_plain   codemap=false  use=forbidden")
    )
    monkeypatch.setattr(readcrop_stage, "run_stage", lambda **_kwargs: None)
    monkeypatch.setattr(fix_stage, "run_fix_stage", lambda study, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "write_checksums", lambda _path: None)

    script_run_codex._run_unified_execution(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        reasoning_effort=script_run_codex.PARITY_CODEX_REASONING_EFFORT,
        tasks="FN-02",
        manifest_path=BENCHMARKS_DIR / "manifests" / "codex-structural.json",
        index_path=tmp_path / "index.json",
        marketplace_root=tmp_path,
        codemap_bin=tmp_path / "codemap-py",
        auth_source=None,
        invocation_launcher_path=None,
        run_dir=None,
        paid_approval=None,
        dry_run=True,
        show_legend=False,
        show_paid_command=show_paid_command,
    )

    output = capsys.readouterr().out
    assert ("PAID_COMMAND:" in output) is paid_block_expected
    assert f"SCOPE   {scope['scope_sha256']}" in output
    assert "DESIGN   1 tasks × A/B/C = 3 cells" in output
    assert "PROBE   A_plain   codemap=false  use=forbidden" in output
