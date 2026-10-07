"""Contracts for the entrypoint the shell launcher renders its framed surfaces through."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARKS))

from _bench_common import render_cli  # noqa: E402
from _bench_common.presentation import LEGEND_CLOSE_RULE, LEGEND_OPEN_RULE  # noqa: E402

RENDER_CLI = BENCHMARKS / "_bench_common" / "render_cli.py"


def _render(arguments: list[str], stdin_text: str = "") -> subprocess.CompletedProcess[str]:
    """Run the entrypoint as the launcher runs it, with its output captured rather than on a terminal.

    Args:
        arguments: Subcommand and its operands, without the program name.
        stdin_text: Text piped to the process, for surfaces that read a body.

    Returns:
        The completed process, with text streams captured.
    """
    return subprocess.run(
        [sys.executable, str(RENDER_CLI), *arguments],
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
    )


def test_redirected_phase_header_keeps_the_line_run_logs_parse() -> None:
    """A header rendered into a redirected stream stays the plain ``== title ==`` line.

    Every launcher run in CI, and every paid run the operator tees into a log, has its stdout redirected. Those logs are
    read back by people and by the result renderer, so a header that gained a rich border or a colour escape when nobody
    was watching a terminal would corrupt the one form both readers already parse.
    """
    completed = _render(["rule", "PREPARE frozen parity index"])

    assert completed.returncode == 0
    assert completed.stdout == "== PREPARE frozen parity index ==\n"
    assert "\x1b[" not in completed.stdout


def test_redirected_legend_is_framed_by_the_shared_rules() -> None:
    """A legend body piped in comes back framed by the same rules a runner prints around its own.

    The launcher would pipe a heredoc into this surface, and a heredoc always ends in a newline. If that trailing
    newline reached the frame, the legend would carry a blank last line that the runner-emitted legends beside it do not
    have, which is exactly the inconsistency this entrypoint exists to remove.
    """
    body = "  treatments: A_plain=no Codemap, B_auto=direct Codemap required\n  status: done, failed\n"

    completed = _render(["legend"], stdin_text=body)

    assert completed.returncode == 0
    assert completed.stdout.splitlines() == [
        LEGEND_OPEN_RULE,
        "  treatments: A_plain=no Codemap, B_auto=direct Codemap required",
        "  status: done, failed",
        LEGEND_CLOSE_RULE,
    ]
    assert "\x1b[" not in completed.stdout


def test_body_lines_arriving_with_windows_endings_do_not_widen_the_frame(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Body lines keep no carriage return, whichever host wrote the heredoc feeding them in.

    The launcher and its tests run on Linux, macOS, and native Windows. A body line that kept its ``\\r`` would push the
    panel border a column wide on one host only, so the same legend would not line up with the rows around it there.
    The carriage returns are handed to the reader in-process rather than piped through a child: a text-mode pipe
    translates the ``\\n`` of a ``\\r\\n`` again on Windows, so the child would receive ``\\r\\r\\n`` and the test would
    assert on the harness's own translation instead of on what the reader does with a Windows line ending.
    """
    monkeypatch.setattr(render_cli, "benchmark_console", _raise_missing_rich)

    render_cli.render_legend(render_cli.read_body_lines(["  metrics:\r\n", "      EREC: expected recall\r\n"]))

    assert capsys.readouterr().out == (
        f"{LEGEND_OPEN_RULE}\n  metrics:\n      EREC: expected recall\n{LEGEND_CLOSE_RULE}\n"
    )


def test_header_survives_a_host_without_rich(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A missing rich install degrades the header instead of ending the run that printed it.

    Rich is optional on the machines the launcher runs on, and the launcher runs under ``set -e`` around work that may
    already have spent money. A header is decoration; an exception raised while drawing one would abort a paid study
    over a cosmetic dependency.
    """
    monkeypatch.setattr(render_cli, "benchmark_console", _raise_missing_rich)

    render_cli.render_rule("CODEX MULTI-STRATUM AUTHORIZATION")

    assert capsys.readouterr().out == "== CODEX MULTI-STRATUM AUTHORIZATION ==\n"


def test_legend_survives_a_host_without_rich(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A missing rich install still frames the legend, because its body is not decoration.

    The legend names what every column around it means. Dropping it on a host without rich would leave a reader with
    unlabelled columns, so the fallback prints the same rules and body a redirected run writes to its log.
    """
    monkeypatch.setattr(render_cli, "benchmark_console", _raise_missing_rich)

    render_cli.render_legend(["  status: done, failed"])

    assert capsys.readouterr().out == f"{LEGEND_OPEN_RULE}\n  status: done, failed\n{LEGEND_CLOSE_RULE}\n"


def _raise_missing_rich(*args: object, **kwargs: object) -> object:
    """Stand in for the console builder on a host where rich is not installed.

    Args:
        *args: Ignored console arguments.
        **kwargs: Ignored console options.

    Raises:
        ImportError: Always, the way the lazy rich import fails when the package is absent.
    """
    raise ImportError("No module named 'rich'")


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param([], id="no-subcommand"),
        pytest.param(["rule"], id="rule-without-title"),
        pytest.param(["panel", "TITLE"], id="unknown-subcommand"),
    ],
)
def test_a_malformed_invocation_fails_loudly_rather_than_printing_a_stray_line(arguments: list[str]) -> None:
    """An unusable command line exits non-zero with its usage on stderr and prints nothing to stdout.

    The launcher pipes this entrypoint's stdout into run logs that a result renderer replays. A misspelled subcommand
    that printed a partial or empty line to stdout anyway would inject a line into that stream, so the failure has to
    stay on stderr where the launcher's own fallback and the operator can see it.
    """
    completed = _render(arguments)

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "usage: render_cli.py" in completed.stderr
