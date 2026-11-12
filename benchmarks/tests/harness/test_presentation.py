"""Terminal presentation contracts shared by benchmark providers."""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

BENCHMARKS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCHMARKS))

from _bench_common.presentation import (  # noqa: E402
    benchmark_console,
    format_artifact_block,
    format_paid_command_block,
    format_quality,
    print_legend,
)

_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


@pytest.mark.parametrize("term", ["dumb", "unknown", "xterm-256color"])
@pytest.mark.parametrize("force_color", [False, True])
@pytest.mark.parametrize("legacy_windows", [False, True])
@pytest.mark.parametrize("lines", ["", "40"])
def test_live_legend_panel_is_120_columns(
    monkeypatch: pytest.MonkeyPatch, term: str, force_color: bool, legacy_windows: bool, lines: str
) -> None:
    """Every provider gets a 120-column panel without truncating its legend body.

    A forced-colour console renders one legend body; after the colour codes are stripped, the top rule, body row and
    bottom rule must all span the full 120 columns so provider legends line up on one terminal width.
    """
    stream = io.StringIO()
    monkeypatch.setenv("TERM", term)
    monkeypatch.setenv("LINES", lines)
    monkeypatch.delenv("TTY_COMPATIBLE", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr("rich.console.detect_legacy_windows", lambda: legacy_windows)
    body = "Legend content " + "x" * 90

    print_legend([body], console=benchmark_console(file=stream, force_color=force_color))

    visible = _ANSI_SGR.sub("", stream.getvalue()).splitlines()
    assert {len(line) for line in visible} == {120}
    assert len(visible) == 3
    assert body in visible[1]


def test_multiple_artifacts_render_as_a_readable_list() -> None:
    """Two durable paths must not be compressed into one unscannable terminal line.

    A run reports its telemetry and metadata files together; the block lists each as its own `name=path` row so an
    operator can copy either path without splitting a long line.
    """
    block = format_artifact_block(telemetry="results/run/telemetry.jsonl", metadata="results/run/run-metadata.json")

    assert block == "ARTIFACTS:\n - telemetry=results/run/telemetry.jsonl\n - metadata=results/run/run-metadata.json"


def test_paid_command_is_framed_so_its_first_line_cannot_read_as_the_label() -> None:
    """The copyable command is delimited above and below, not merely announced by a label.

    A paid command frequently opens with an upper-case environment assignment, which on a terminal looks exactly like
    the ``PAID_COMMAND`` label printed above it. Without the rules an operator reads the label as part of the command,
    or the assignment as another heading, and copies the wrong span into a run that spends money.
    """
    block = format_paid_command_block(
        [
            "CODEMAP_BENCH_PATCH_PYTEST=/venv/bin/pytest python3 benchmarks/run-codex-structural.py \\",
            "  --paid-approval 71bb01f80ac200c1",
        ]
    )

    lines = block.splitlines()
    assert lines[0] == "PAID_COMMAND:"
    assert lines[1] == "-" * 78
    assert lines[2].startswith("CODEMAP_BENCH_PATCH_PYTEST=")
    assert lines[-1] == "-" * 78


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        pytest.param(1.0, "1.000 ", id="perfect"),
        pytest.param(0.258, "0.258 ", id="fraction"),
        pytest.param(0.0, "0.000 ", id="zero"),
    ],
)
def test_quality_format_preserves_the_shared_column_width(score: float, expected: str) -> None:
    """Scores retain fixed-width alignment without duplicating row eligibility state.

    Every score renders as three decimals plus one trailing space, so result tables from different providers keep their
    columns aligned whether the score is a perfect 1, a fraction or zero.
    """
    assert format_quality(score) == expected
