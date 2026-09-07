"""Shared terminal formatting and Rich progress construction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import re
from typing import Any


#: Label introducing the one command a dry run authorizes for paid execution.
PAID_COMMAND_LABEL = "PAID_COMMAND:"
#: Width of the rules framing a paid command, wide enough for a flag line without wrapping.
PAID_COMMAND_RULE_WIDTH = 78
#: Terminal legends and section rules share a fixed width; archived markers and copyable command
#: rules retain their existing width for log compatibility.
BENCHMARK_OUTPUT_WIDTH = 120


def titled_rule(title: str, *, character: str = "=", width: int = PAID_COMMAND_RULE_WIDTH) -> str:
    """Return one full-width rule carrying a centered title.

    A legend and a paid command are both blocks a reader has to find inside a long run log, so both
    are framed the same way and differ only in the rule character. An odd remainder goes to the
    right, which keeps the opening and closing rules of one block the same length.

    Args:
        title: The word or phrase named in the middle of the rule.
        character: The rule's fill character.
        width: The rule's total width in characters.

    Returns:
        The rule line, without a trailing newline.

    Examples:
        >>> titled_rule("LEGEND", width=20)
        '====== LEGEND ======'
        >>> titled_rule("END LEGEND", width=20)
        '==== END LEGEND ===='
    """
    label = f" {title} "
    fill = max(width - len(label), 2)
    left = fill // 2
    return f"{character * left}{label}{character * (fill - left)}"


#: Rules opening and closing the legend block, distinguishing it from surrounding stream output.
LEGEND_OPEN_RULE = titled_rule("LEGEND")
LEGEND_CLOSE_RULE = titled_rule("END LEGEND")

ARM_ROW_STYLES = {
    "A_plain": "yellow",
    "B_auto": "cyan",
    "C_strict": "magenta",
}

_PLAN_ROW_ARM = re.compile(r"^(?:PLAN|PROBE)\b.*?\b(A_plain|B_auto|C_strict)\b")


def format_artifact_block(**artifacts: str | Path) -> str:
    """Format two or more durable artifact paths as a scannable terminal block.

    Args:
        **artifacts: Ordered artifact labels and their persisted paths.

    Returns:
        Plain, ANSI-free terminal text with one labeled artifact per line.

    Raises:
        ValueError: If fewer than two artifacts are supplied.

    Examples:
        >>> print(format_artifact_block(report="report.json", log="run.log"))
        ARTIFACTS:
         - report=report.json
         - log=run.log
        >>> format_artifact_block(report="report.json")
        Traceback (most recent call last):
        ...
        ValueError: an artifact block requires at least two labeled paths
    """
    if len(artifacts) < 2:
        raise ValueError("an artifact block requires at least two labeled paths")
    return "ARTIFACTS:\n" + "\n".join(f" - {label}={path}" for label, path in artifacts.items())


def format_paid_command_block(command_lines: Sequence[str]) -> str:
    """Frame the one command a dry run authorizes between two rules.

    The command is what the operator copies, and it may begin with an upper-case environment
    assignment that reads exactly like the ``PAID_COMMAND`` label above it. The rule extending the
    label, and the closing rule under the last flag, mark where the copyable region starts and ends
    so the two cannot be confused for one another.

    Args:
        command_lines: The command's lines, already quoted and continuation-escaped.

    Returns:
        The framed block, without a trailing newline.

    Raises:
        ValueError: If no command lines are supplied.

    Examples:
        >>> block = format_paid_command_block(["python3 run.py \\\\", "  --paid-approval abc123"])
        >>> block.splitlines()[0]
        'PAID_COMMAND:'
        >>> block.splitlines()[1] == "-" * 78
        True
        >>> block.splitlines()[2:4]
        ['python3 run.py \\\\', '  --paid-approval abc123']
        >>> block.splitlines()[-1] == "-" * 78
        True
        >>> format_paid_command_block([])
        Traceback (most recent call last):
        ...
        ValueError: a paid command block requires at least one command line
    """
    if not command_lines:
        raise ValueError("a paid command block requires at least one command line")
    rule = "-" * PAID_COMMAND_RULE_WIDTH
    return "\n".join([PAID_COMMAND_LABEL, rule, *command_lines, rule])


def format_quality(quality: float | None) -> str:
    """Round a score to three decimals and pad to a minimum width of six.

    Use ``?`` for missing scores. Values are formatted without clamping to the
    expected score range; unusually wide values are not truncated.

    Args:
        quality: Score in the benchmark's continuous [0, 1] range, if available.
    Returns:
        A padded display value such as ``"1.000 "`` or ``"0.258 "``.

    Examples:
        >>> format_quality(0.25)
        '0.250 '
        >>> format_quality(None)
        '?     '
    """
    score = "?" if quality is None else f"{float(quality):.3f}"
    return score.ljust(6)


def fmt_tok(v: float) -> str:
    """Format a token count with a k/M unit suffix.

    Millions get one decimal (``1.5M``); anything ``>=1000`` becomes ``k`` (``937.6k``);
    smaller counts print raw (``842``). Used for both input and output token columns so
    they read consistently across runners.

    Args:
        v: Token count.

    Returns:
        Formatted count, e.g. ``"1.5M"``, ``"937.6k"``, or ``"842"``.

    Examples:
        >>> fmt_tok(1_500_000)
        '1.5M'
        >>> fmt_tok(937_600)
        '937.6k'
        >>> fmt_tok(842)
        '842'
    """
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1000:
        return f"{v / 1000:.1f}k"
    return f"{int(v)}"


def fmt_time(seconds: float) -> str:
    """Format an elapsed duration as ``2m5s`` (minutes + seconds).

    The minute part is dropped below one minute (``45s``); seconds are rounded to the nearest
    integer. Used for the per-run and progress time columns so they read consistently across runners.

    Args:
        seconds: Elapsed wall-clock seconds.

    Returns:
        ``"<m>m<s>s"`` at or above a minute, else ``"<s>s"``.

    Examples:
        >>> fmt_time(125)
        '2m5s'
        >>> fmt_time(45.4)
        '45s'
        >>> fmt_time(0)
        '0s'
    """
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m{secs}s" if minutes else f"{secs}s"


def print_arm_row(row: str, arm: str, *, console: Any) -> None:
    """Render one benchmark arm row with Rich color only on interactive terminals.

    Args:
        row: Fully formatted plain-text result row.
        arm: Canonical benchmark arm label.
        console: Rich console configured by the provider runner.

    Raises:
        ValueError: If the row uses an unknown benchmark arm.
    """
    try:
        style = ARM_ROW_STYLES[arm]
    except KeyError as exc:
        raise ValueError(f"unknown benchmark arm {arm!r}") from exc
    if console.is_terminal:
        console.print(row, style=style, markup=False, highlight=False, soft_wrap=True)
        return
    print(row)


def plan_row_arm(row: str) -> str | None:
    """Return the benchmark arm a no-model ``PLAN`` or ``PROBE`` row announces.

    Args:
        row: One terminal row emitted by a dry-run planner or capability probe.

    Returns:
        The arm label exactly as the row spells it, or ``None`` for a row that names no arm.

    Examples:
        >>> plan_row_arm("PLAN    FN-02  rep=1  C_strict")
        'C_strict'
        >>> plan_row_arm("PROBE   B_auto       codemap=true")
        'B_auto'
        >>> plan_row_arm("READCROP PREFLIGHT (no model)") is None
        True
    """
    match = _PLAN_ROW_ARM.match(row)
    return match.group(1) if match else None


def print_plan_row(row: str, *, console: Any) -> None:
    """Render one no-model plan or probe row, colored by arm on interactive terminals.

    Rows that name no arm, such as a stage preflight banner, print unchanged.

    Args:
        row: Fully formatted plain-text plan or probe row.
        console: Rich console configured by the provider runner.
    """
    arm = plan_row_arm(row)
    if arm is None:
        print(row)
        return
    print_arm_row(row, arm, console=console)


def benchmark_console(file: Any = None, *, force_color: bool = False) -> Any:
    """Build the one console every benchmark surface renders through.

    Terminal legends and section rules use :data:`BENCHMARK_OUTPUT_WIDTH` regardless of the window
    width. Plain log markers and paid-command rules retain their existing width and ANSI-free form
    for compatibility with archived streams and downstream parsers.

    Args:
        file: Stream to render into; ``None`` uses the console's own default of stdout.
        force_color: Render terminal styling even when the stream is not a TTY.

    Returns:
        A configured rich ``Console``.
    """
    from rich.console import Console

    return Console(
        file=file,
        force_terminal=True if force_color else None,
        highlight=False,
        markup=False,
        soft_wrap=False,
        width=BENCHMARK_OUTPUT_WIDTH,
    )


def print_legend(body_lines: Sequence[str], *, console: Any) -> None:
    """Render one legend as a titled panel on a terminal and as titled rules elsewhere.

    The plain form is what a redirected run writes to its log, and it is also what the result
    renderer recognizes and upgrades to a panel when a stored stream is replayed to a terminal.

    Args:
        body_lines: The legend's lines, without the framing rules.
        console: Console from :func:`benchmark_console`.
    """
    from rich.panel import Panel

    body = "\n".join(line.rstrip("\r\n") for line in body_lines)
    if console.is_terminal:
        console.print(Panel(body, title="Legend", subtitle="End legend", border_style="blue", width=console.width))
        return
    print(LEGEND_OPEN_RULE)
    print(body)
    print(LEGEND_CLOSE_RULE)


def print_section_rule(title: str, *, console: Any) -> None:
    """Announce one run phase as a titled rule on a terminal and as ``== title ==`` elsewhere.

    Args:
        title: The phase name, without its surrounding marks.
        console: Console from :func:`benchmark_console`.
    """
    from rich.rule import Rule

    if console.is_terminal:
        console.print(Rule(title, style="blue"))
        return
    print(f"== {title} ==")


def format_probe_row(arm: str, fields: Mapping[str, Any]) -> str:
    """Format one capability probe row with its fields in fixed columns.

    Probe rows were tab-separated, so a long arm name pushed its own fields a stop to the right and
    no column lined up with the row above it. Every field is padded here instead. Field names are
    written exactly as given, because each one is part of a row format its readers already parse.

    Args:
        arm: Canonical benchmark arm label.
        fields: Ordered probe field names and values; booleans render lower-case.

    Returns:
        The plain, ANSI-free row, without a trailing newline.

    Examples:
        >>> format_probe_row("A_plain", {"codemap": False, "codemap_python": "absent"})
        'PROBE   A_plain   codemap=false  codemap_python=absent'
        >>> format_probe_row("C_strict", {"codemap": True, "skill-required": True})
        'PROBE   C_strict  codemap=true   skill-required=true'
    """
    rendered = []
    for label, value in fields.items():
        text = str(value).lower() if isinstance(value, bool) else str(value)
        rendered.append(f"{label}={text}")
    # The last field is whatever remains of the line, so padding it would only add trailing blanks.
    padded = [f"{field:<14}" for field in rendered[:-1]] + rendered[-1:]
    return f"PROBE   {arm:<9} " + " ".join(padded)


def make_progress(console: Any):
    """Build the standard four-column Rich progress display for runner live bars.

    ``rich`` is imported lazily so this module stays importable where rich is optional
    (the CLI runner guards its rich import).

    Args:
        console: The rich ``Console`` to render into.

    Returns:
        A configured ``rich.progress.Progress`` (spinner · description · bar · percent).
    """
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    )
