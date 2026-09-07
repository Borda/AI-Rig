"""Terminal presentation contracts shared by benchmark providers."""

from __future__ import annotations

import io
from pathlib import Path
import re
import sys


BENCHMARKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARKS))

from _bench_common.presentation import (  # noqa: E402
    benchmark_console,
    format_artifact_block,
    format_paid_command_block,
    format_quality,
    print_legend,
)


def test_live_legend_panel_is_120_columns() -> None:
    """Every provider gets a 120-column panel without truncating its legend body."""
    stream = io.StringIO()
    body = "Legend content " + "x" * 90

    print_legend([body], console=benchmark_console(file=stream, force_color=True))

    visible = re.sub(r"\x1b\[[0-9;]*m", "", stream.getvalue()).splitlines()
    assert {len(line) for line in visible} == {120}
    assert len(visible) == 3
    assert body in visible[1]


def test_multiple_artifacts_render_as_a_readable_list() -> None:
    """Two durable paths must not be compressed into one unscannable terminal line."""
    assert format_artifact_block(telemetry="results/run/telemetry.jsonl", metadata="results/run/run-metadata.json") == (
        "ARTIFACTS:\n - telemetry=results/run/telemetry.jsonl\n - metadata=results/run/run-metadata.json"
    )


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


def test_quality_format_preserves_the_shared_column_width() -> None:
    """Scores retain fixed-width alignment without duplicating row eligibility state."""
    assert format_quality(1.0) == "1.000 "
    assert format_quality(0.258) == "0.258 "
    assert format_quality(0.0) == "0.000 "
