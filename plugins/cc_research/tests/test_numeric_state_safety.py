"""Cover bounded numeric input handling and atomic counter-state replacement."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import codex_state
import compute_effect_size as effects
import fortify_metric_stats as stats
import pytest


def _symlinks_supported() -> bool:
    """Report whether the host permits unprivileged symlink creation."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / "link").symlink_to(Path(directory) / "target")
        except OSError:
            return False
    return True


@pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")
def test_counter_bump_leaves_planted_temp_target_unchanged(tmp_path: Path) -> None:
    """Replace the state entry without writing through a predictable temporary symlink."""
    victim = tmp_path / "victim"
    victim.write_text("keep me", encoding="utf-8")
    state = tmp_path / "codex-state"
    state.with_suffix(".tmp").symlink_to(victim)

    assert codex_state.bump(state, "noop") == (1, 1)

    assert victim.read_text(encoding="utf-8") == "keep me"
    assert not state.is_symlink()
    assert codex_state.load_counters(state) == (1, 1)


@pytest.mark.parametrize(
    "payload",
    [
        '{"codex_iter": Infinity}',
        pytest.param("[" * 10000 + "0" + "]" * 10000, id="deep-json"),
    ],
)
def test_unusable_counter_numbers_reset_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: str
) -> None:
    """Treat infinite counters and excessively nested JSON as unusable state."""
    state = tmp_path / "codex-state"
    state.write_text(payload, encoding="utf-8")

    assert codex_state.load_counters(state) == (0, 0)
    assert "unusable" in capsys.readouterr().err


@pytest.mark.parametrize("statistic", [float("nan"), float("inf"), -1.0, 37.0])
def test_effect_size_rejects_impossible_statistics(statistic: float) -> None:
    """Reject nonfinite statistics and signed-rank sums outside the possible range."""
    with pytest.raises(ValueError, match="statistic"):
        effects.rank_biserial_r(statistic, 8)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b'{"n":' + b"1" + b"0" * 400 + b',"statistic":1}', id="huge-n"),
        pytest.param(b"[" * 10000 + b"0" + b"]" * 10000, id="deep-json"),
        pytest.param(b"\xff", id="non-utf8"),
        pytest.param(b'{"n":' + b"1" * 5000 + b',"statistic":1}', id="integer-digit-limit"),
    ],
)
def test_effect_size_cli_reports_input_errors_without_traceback(payload: bytes) -> None:
    """Return the documented input-error exit code for malformed or excessive numeric input."""
    result = subprocess.run(
        [sys.executable, effects.__file__],
        input=payload,
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8:strict"},
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == b""
    assert b"error:" in result.stderr
    assert b"Traceback" not in result.stderr


def test_metric_ledger_over_cap_blocks_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Block an oversized run ledger instead of interpreting it as absent metric history."""
    monkeypatch.setattr(stats, "MAX_JSONL_BYTES", 24, raising=False)
    (tmp_path / "metric-runs.jsonl").write_bytes(b'{"variant":"full","metric":1}\n')

    assert stats.main(["status", str(tmp_path), "--variant", "full", "--runs", "1"]) == 1
    assert "exceeds 24 bytes" in capsys.readouterr().out


def test_metric_ledger_at_cap_remains_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept a ledger exactly at the byte cap to distinguish the size boundary."""
    path = tmp_path / "metric-runs.jsonl"
    path.write_bytes(b'{"metric":1}\n')
    monkeypatch.setattr(stats, "MAX_JSONL_BYTES", path.stat().st_size, raising=False)

    assert stats._read_jsonl(path) == [{"metric": 1}]


@mock.patch("fortify_metric_stats.cmd_status", autospec=True, side_effect=ValueError("unexpected handler failure"))
def test_unexpected_handler_value_error_propagates(mock_status: mock.MagicMock, tmp_path: Path) -> None:
    """Unexpected handler errors remain visible instead of becoming ledger-size diagnostics."""
    with pytest.raises(ValueError, match="unexpected handler failure"):
        stats.main(["status", str(tmp_path), "--variant", "full", "--runs", "1"])


def test_non_utf8_metric_ledger_blocks_status(tmp_path: Path) -> None:
    """Invalid ledger encoding remains a handled input error."""
    (tmp_path / "metric-runs.jsonl").write_bytes(b"\xff")
    assert stats.main(["status", str(tmp_path), "--variant", "full", "--runs", "1"]) == 1
