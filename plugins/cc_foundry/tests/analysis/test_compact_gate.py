"""Test boundary compaction decisions, transcript evidence, portable paths, and fixed shell invocation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any

import compact_gate as gate
import cost_analyzer as ca
import pytest
from _hook_env import _bash_runs_posix_script


def _usage_row(
    message_id: str,
    model: str = "claude-opus-5-5",
    ctx: int = 250_000,
    write_5m: int = 0,
    write_1h: int = 2_000,
) -> dict[str, Any]:
    """Construct a realistic assistant row whose prompt plus output equals the requested context."""
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "model": model,
            "content": [],
            "usage": {
                "input_tokens": 17_000,
                "cache_read_input_tokens": ctx - 17_000 - 1_000 - write_5m - write_1h,
                "cache_creation_input_tokens": write_5m + write_1h,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": write_5m,
                    "ephemeral_1h_input_tokens": write_1h,
                },
                "output_tokens": 1_000,
            },
        },
    }


def _write_transcript(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write isolated usage evidence with the same JSONL framing as a session transcript."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\n".join(json.dumps(row) for row in rows) + "\n").encode("utf-8"))


def _write_contract(raw: bytes) -> None:
    """Create a complete contract and owner for the isolated current session."""
    gate.CONTRACT_PATH.write_bytes(raw)
    gate.CONTRACT_PATH.with_suffix(".owner").write_bytes(
        (json.dumps({"session_id": "sample-session", "sha256": hashlib.sha256(raw).hexdigest()}) + "\n").encode()
    )


@pytest.fixture
def gate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Provide an attended boundary and main transcript in an isolated home directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sample-session")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ATTENDED", "1")
    # A host opt-in would turn every paying boundary into stop; tests own the switch explicitly.
    monkeypatch.delenv(gate.STOP_ENV, raising=False)
    gate.CONTRACT_PATH.parent.mkdir(parents=True)
    _write_contract(b"- gate-metadata:\n    - calls_ahead: 60\n    - drop: scratch notes\n")
    transcript = home / ".claude" / "projects" / "sample-project" / "sample-session.jsonl"
    _write_transcript(transcript, [_usage_row("latest")])
    return home, transcript


def _run_gate(home: Path, capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, Any], str]:
    """Check the decision-line CLI contract and return its corresponding appended event."""
    assert gate.main([]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("compact-gate: ")
    assert captured.out.count("compact-gate: ") == 1
    rows = (home / ".claude" / "logs" / "compactions.jsonl").read_text(encoding="utf-8").splitlines()
    return json.loads(rows[-1]), captured.out.splitlines()[0]


@pytest.mark.parametrize(
    ("calls_ahead", "stop_env", "decision"),
    [
        pytest.param(1, "stop", "continue", id="not-paying-ignores-opt-in"),
        pytest.param(60, None, "advise", id="paying-default-advises"),
        pytest.param(60, "1", "advise", id="unknown-value-advises"),
        pytest.param(60, "stop", "stop", id="paying-opt-in-stops"),
        pytest.param(60, " Stop ", "stop", id="opt-in-tolerates-case-and-space"),
    ],
)
def test_decision_is_one_line_and_stop_is_opt_in(
    gate_state: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    calls_ahead: int,
    stop_env: str | None,
    decision: str,
) -> None:
    """A paying boundary advises unless FOUNDRY_COMPACT_GATE=stop opts into holding the run; output is one line.

    Holding the run by default left skills idle at every boundary past ~250K context, and the echoed contract fed a
    paste line too long to survive a terminal copy. Only the explicit opt-in may stop, and nothing follows the line.
    """
    home, _ = gate_state
    _write_contract(f"- next: resume step\n- gate-metadata:\n    - calls_ahead: {calls_ahead}\n".encode())
    if stop_env is not None:
        monkeypatch.setenv(gate.STOP_ENV, stop_env)
    row, line = _run_gate(home, capsys)
    assert row["decision"] == decision
    assert line.startswith(f"compact-gate: {decision} ")


@pytest.mark.parametrize(
    ("calls_ahead", "decision"),
    [pytest.param(44, "continue", id="below-break-even"), pytest.param(45, "advise", id="above-break-even")],
)
def test_worked_threshold(
    gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str], calls_ahead: int, decision: str
) -> None:
    """The measured Opus-class example changes decision between 44 and 45 remaining calls."""
    home, _ = gate_state
    _write_contract(f"- gate-metadata:\n    - calls_ahead: {calls_ahead}\n".encode())
    rate = ca.PRICES["opus-5.5"].rate(249_000)
    write_read = rate.input * ca.CACHE_WRITE_1H_MULTIPLIER / rate.cache_read
    input_read, output_read = rate.input / rate.cache_read, rate.output / rate.cache_read
    assert (write_read, input_read, output_read) == (40, 20, 100)
    expected_k = 120_000 * write_read + 250_000 + 10_000 * input_read + 5_000 * output_read
    assert expected_k == 5_750_000
    row, line = _run_gate(home, capsys)
    assert row["decision"] == decision
    assert row["ctx"] == 250_000
    assert row["K"] == expected_k
    assert row["calls_ahead"] == calls_ahead
    assert line == (
        f"compact-gate: {decision} ctx=250000 floor=120000 calls_ahead={calls_ahead} "
        f"K=5750000.0 saving={130_000 * calls_ahead}"
    )


@pytest.mark.parametrize("key", sorted(ca.PRICES))
def test_every_priced_model(gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str], key: str) -> None:
    """Every current PRICES entry resolves through its real transcript model spelling."""
    home, transcript = gate_state
    model = f"claude-{key.replace('.', '-')}-20260101[1m]"
    _write_transcript(transcript, [_usage_row("latest", model=model)])
    row, _ = _run_gate(home, capsys)
    rate = ca.PRICES[key].rate(249_000)
    expected = (
        120_000 * rate.input * ca.CACHE_WRITE_1H_MULTIPLIER / rate.cache_read
        + 250_000
        + 10_000 * rate.input / rate.cache_read
        + 5_000 * rate.output / rate.cache_read
    )
    assert row["model"] == model
    assert row["K"] == expected
    assert row["decision"] == ("advise" if 130_000 * 60 > expected else "continue")


@pytest.mark.parametrize(
    ("cause", "reason"),
    [
        pytest.param("contract", "no contract", id="contract-missing"),
        pytest.param("calls", "no calls_ahead", id="calls-missing"),
        pytest.param("interactive", "interactive: false in contract", id="contract-noninteractive"),
        pytest.param("attended", "session unattended", id="unattended-session"),
        pytest.param("session", "no session id", id="session-id-missing"),
        pytest.param("transcript", "transcript missing", id="transcript-missing"),
        pytest.param("usage", "no usage", id="usage-missing"),
        pytest.param("model", "unknown model", id="model-unpriced"),
    ],
)
def test_continue_causes(
    gate_state: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cause: str,
    reason: str,
) -> None:
    """Each missing-evidence or attendance condition produces a successful reasoned continue."""
    home, transcript = gate_state
    if cause == "contract":
        gate.CONTRACT_PATH.unlink()
    elif cause == "calls":
        _write_contract(b"- gate-metadata:\n    - drop: notes\n")
    elif cause == "interactive":
        _write_contract(b"- gate-metadata:\n    - calls_ahead: 60\n    - interactive: false\n")
    elif cause == "attended":
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ATTENDED", "0")
    elif cause == "session":
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    elif cause == "transcript":
        transcript.unlink()
    elif cause == "usage":
        _write_transcript(transcript, [{"type": "user", "message": {"content": "hello"}}])
    else:
        _write_transcript(transcript, [_usage_row("latest", model="claude-opus-99")])
    row, line = _run_gate(home, capsys)
    assert row["decision"] == "continue"
    assert row["reason"] == reason
    assert row["K"] is None
    assert line == f"compact-gate: continue ({reason})"


def test_deduplication_and_weighted_split(gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """Repeated content blocks neither inflate write weights nor replace the last unique call."""
    home, transcript = gate_state
    older = _usage_row("older", model="claude-sonnet-4", ctx=180_000, write_5m=6_000, write_1h=0)
    latest = _usage_row("latest")
    _write_transcript(transcript, [older, older, latest, older])
    # A larger child transcript must never become the main-session context or write estimate.
    _write_transcript(
        transcript.parent / transcript.stem / "subagents" / "agent-child.jsonl", [_usage_row("child", ctx=900_000)]
    )
    row, _ = _run_gate(home, capsys)
    multiplier = (6_000 * ca.CACHE_WRITE_5M_MULTIPLIER + 2_000 * ca.CACHE_WRITE_1H_MULTIPLIER) / 8_000
    rate = ca.PRICES["opus-5.5"].base
    expected = 120_000 * rate.input / rate.cache_read * multiplier + 250_000 + 10_000 * 20 + 5_000 * 100
    assert row["ctx"] == 250_000
    assert row["model"] == "claude-opus-5-5"
    assert row["K"] == expected


@pytest.mark.parametrize("legacy", [False, True])
def test_five_minute_fallback(gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str], legacy: bool) -> None:
    """Old unsplit writes and a session without writes both use the analyzer's five-minute rate."""
    home, transcript = gate_state
    usage = _usage_row("latest", write_5m=2_000 if legacy else 0, write_1h=0)
    del usage["message"]["usage"]["cache_creation"]
    _write_transcript(transcript, [usage])
    row, _ = _run_gate(home, capsys)
    rate = ca.PRICES["opus-5.5"].base
    assert row["K"] == 120_000 * rate.input * ca.CACHE_WRITE_5M_MULTIPLIER / rate.cache_read + 950_000


@pytest.mark.parametrize(
    ("ctx", "calls"),
    [
        pytest.param(120_000, 60, id="at-floor"),
        pytest.param(100_000, 60, id="below-floor"),
        pytest.param(250_000, 0, id="no-remaining-calls"),
        pytest.param(260_500, 41, id="exact-break-even"),
    ],
)
def test_strict_saving_boundary(
    gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str], ctx: int, calls: int
) -> None:
    """Zero or negative savings and equality with rebuild cost never trigger compaction."""
    home, transcript = gate_state
    _write_contract(f"- gate-metadata:\n    - calls_ahead: {calls}\n".encode())
    _write_transcript(transcript, [_usage_row("latest", ctx=ctx)])
    row, _ = _run_gate(home, capsys)
    assert row["decision"] == "continue"
    assert (ctx - 120_000) * calls <= row["K"]
    if ctx == 260_500:
        assert (ctx - 120_000) * calls == row["K"]


def test_log_shape_and_append(gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """A gate appends its exact evidence schema and preserves all earlier rows."""
    home, _ = gate_state
    log = home / ".claude" / "logs" / "compactions.jsonl"
    log.parent.mkdir(parents=True)
    prior = b'{"event":"earlier"}\n'
    log.write_bytes(prior)
    row, _ = _run_gate(home, capsys)
    assert log.read_bytes().startswith(prior)
    assert len(log.read_bytes().splitlines()) == 2
    assert set(row) == {"event", "session_id", "decision", "reason", "ctx", "calls_ahead", "K", "model", "ts"}
    assert row["event"] == "gate"
    assert row["session_id"] == "sample-session"
    assert datetime.fromisoformat(row["ts"]).tzinfo is not None


@pytest.mark.parametrize("failure", ["mkdir", "open", "write"])
def test_unwritable_log_keeps_decision(
    gate_state: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    """Directory, open, and append failures cannot change a successful one-line decision."""
    home, _ = gate_state
    if failure == "mkdir":
        (home / ".claude" / "logs").write_bytes(b"not a directory")
    else:
        original_open = Path.open

        def blocked_open(path: Path, *args: Any, **kwargs: Any) -> Any:
            """Reject only the telemetry file while permitting contract and transcript reads."""
            if path.name != "compactions.jsonl":
                return original_open(path, *args, **kwargs)
            if failure == "open":
                raise PermissionError("log is unwritable")
            return original_open(path, "r", encoding="utf-8")

        log = home / ".claude" / "logs" / "compactions.jsonl"
        log.parent.mkdir(parents=True)
        log.touch()
        monkeypatch.setattr(Path, "open", blocked_open)
    assert gate.main([]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines()[0] == (
        "compact-gate: advise ctx=250000 floor=120000 calls_ahead=60 K=5750000.0 saving=7800000"
    )


@pytest.mark.parametrize("argument", ["--help", "-h", "--", "unexpected", ""])
def test_every_argument_is_usage_error(
    gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str], argument: str
) -> None:
    """The fixed argument-free interface rejects even help flags without producing a decision."""
    home, _ = gate_state
    assert gate.main([argument]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "usage: compact_gate.py (no arguments)\n"
    assert not (home / ".claude" / "logs").exists()


def test_only_exact_metadata_shapes(gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """Prose, indentation, invalid numbers, and extra interactive text cannot act as gate metadata."""
    home, _ = gate_state
    _write_contract(
        b"calls_ahead: 100\n    - calls_ahead: 100\n- calls_ahead: -1\n- calls_ahead: 60 extra\n"
        b"- calls_ahead: 60\n- interactive: false extra\n- gate-metadata:\n    - calls_ahead: 60\n    - drop: \n"
    )
    row, _ = _run_gate(home, capsys)
    assert row["decision"] == "advise"
    assert row["calls_ahead"] == 60


def test_windows_style_home(
    gate_state: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Windows pathlib coordinates reach the native filesystem through portable path joins."""
    home, transcript = gate_state
    windows_home = PureWindowsPath("C:/Users/Sample User")

    def windows_glob(path: PureWindowsPath, pattern: str) -> list[Path]:
        """Check the Windows search coordinate before exposing the native transcript."""
        assert path == PureWindowsPath("C:/Users/Sample User/.claude/projects")
        assert pattern == "*/sample-session.jsonl"
        return [transcript]

    def windows_mkdir(path: PureWindowsPath, **kwargs: Any) -> None:
        """Map only the expected Windows log directory into the isolated home."""
        assert path == PureWindowsPath("C:/Users/Sample User/.claude/logs")
        (home / ".claude" / "logs").mkdir(**kwargs)

    def windows_open(path: PureWindowsPath, *args: Any, **kwargs: Any) -> Any:
        """Map only the expected Windows append target into a native temporary file."""
        assert path == PureWindowsPath("C:/Users/Sample User/.claude/logs/compactions.jsonl")
        return (home / ".claude" / "logs" / "compactions.jsonl").open(*args, **kwargs)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: windows_home))
    monkeypatch.setattr(PureWindowsPath, "glob", windows_glob, raising=False)
    monkeypatch.setattr(PureWindowsPath, "mkdir", windows_mkdir, raising=False)
    monkeypatch.setattr(PureWindowsPath, "open", windows_open, raising=False)
    row, _ = _run_gate(home, capsys)
    assert row["ctx"] == 250_000
    assert row["decision"] == "advise"


@pytest.mark.integration
@pytest.mark.skipif(
    not _bash_runs_posix_script(), reason="Bash cannot execute POSIX scripts for the fixed boundary block"
)
@pytest.mark.parametrize("present", [False, True])
def test_fixed_bash_block(gate_state: tuple[Path, Path], tmp_path: Path, present: bool) -> None:
    """The verbatim gate-present and gate-absent shell blocks each print exactly one decision line."""
    home, _ = gate_state
    script_dir = tmp_path / "bin"
    script_dir.mkdir()
    if present:
        source_dir = Path(gate.__file__).parent
        for name in ("compact_gate.py", "cost_analyzer.py", "_context_composition.py", "_codex_composition.py"):
            shutil.copyfile(source_dir / name, script_dir / name)
    # A fixture-owned python launcher retains the project's actual interpreter on every host.
    (script_dir / "python").write_bytes(b'#!/usr/bin/env bash\nexec "$_GATE_PYTHON" "$@"\n')
    (script_dir / "python").chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "_GD": str(script_dir),
        "_GATE_PYTHON": sys.executable,
        "PATH": str(script_dir) + os.pathsep + os.environ.get("PATH", ""),
    }
    block = '[ -f "$_GD/compact_gate.py" ] && python "$_GD/compact_gate.py" || echo "compact-gate: continue (foundry gate unavailable)"'
    result = subprocess.run(
        [shutil.which("bash"), "-c", block], cwd=tmp_path, env=env, capture_output=True, encoding="utf-8", timeout=30
    )
    assert result.returncode == 0
    assert result.stderr == ""
    expected = (
        "compact-gate: advise ctx=250000 floor=120000 calls_ahead=60 K=5750000.0 saving=7800000"
        if present
        else "compact-gate: continue (foundry gate unavailable)"
    )
    assert result.stdout == expected + "\n"


@pytest.mark.parametrize("owner", [None, "malformed", "foreign", "mismatch"])
def test_contract_ownership_required(
    gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str], owner: str | None
) -> None:
    """A decision requires an intact owner for the exact bytes and current session."""
    home, _ = gate_state
    raw = b"- gate-metadata:\n    - calls_ahead: 60\n"
    gate.CONTRACT_PATH.write_bytes(raw)
    owner_path = gate.CONTRACT_PATH.with_suffix(".owner")
    if owner is None:
        owner_path.unlink(missing_ok=True)
    elif owner == "malformed":
        owner_path.write_bytes(b"not json")
    else:
        owner_path.write_text(
            json.dumps(
                {
                    "session_id": "other" if owner == "foreign" else "sample-session",
                    "sha256": hashlib.sha256(raw).hexdigest() if owner != "mismatch" else "0" * 64,
                }
            )
        )
    row, line = _run_gate(home, capsys)
    reason = "contract written by another session" if owner == "foreign" else "unverified contract"
    assert row["decision"] == "continue"
    assert line == f"compact-gate: continue ({reason})"


def test_prose_cannot_supply_metadata(gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """Verified prose containing old metadata shapes cannot trigger compaction."""
    home, _ = gate_state
    raw = b"- preserve: example\n- calls_ahead: 999\n- drop: notes\n"
    gate.CONTRACT_PATH.write_bytes(raw)
    gate.CONTRACT_PATH.with_suffix(".owner").write_text(
        json.dumps({"session_id": "sample-session", "sha256": hashlib.sha256(raw).hexdigest()})
    )
    row, line = _run_gate(home, capsys)
    assert row["calls_ahead"] is None
    assert line == "compact-gate: continue (no calls_ahead)"


@pytest.mark.parametrize(
    ("raw", "calls"),
    [
        pytest.param(b"- gate-metadata:\n    - calls_ahead: 999\n- next: prose\n", None, id="not-trailing"),
        pytest.param(b"- gate-metadata:\n- calls_ahead: 999\n", None, id="unindented"),
        pytest.param(b" - gate-metadata:\n    - calls_ahead: 999\n", None, id="inexact-sentinel"),
        pytest.param(
            b"- gate-metadata:\n    - calls_ahead: 999\n- gate-metadata:\n    - drop: notes\n",
            None,
            id="last-block-without-calls",
        ),
        pytest.param(
            b"- gate-metadata:\n    - interactive: false\n- gate-metadata:\n    - calls_ahead: 60\n",
            60,
            id="last-block-wins",
        ),
        pytest.param(b"- gate-metadata:\n    - calls_ahead: 60\n\n", None, id="trailing-blank"),
        pytest.param(b"\xff- gate-metadata:\n    - calls_ahead: 60\n", None, id="invalid-utf8"),
        pytest.param(b"- gate-metadata:\n    - calls_ahead: 1\n    - calls_ahead: 999\n", None, id="duplicate-key"),
        pytest.param(b"- gate-metadata:\n    - calls_ahead: 1000000\n", None, id="oversized-calls"),
        pytest.param(
            "- gate-metadata:\n    - calls_ahead: 1\n    - drop: notes\u2028    - calls_ahead: 999\n".encode(),
            None,
            id="unicode-separator-forgery",
        ),
    ],
)
def test_only_final_metadata_block(
    gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str], raw: bytes, calls: int | None
) -> None:
    """Only an exact final sentinel and its complete indented tail supply gate inputs."""
    home, _ = gate_state
    _write_contract(raw)
    row, line = _run_gate(home, capsys)
    assert row["calls_ahead"] == calls
    if calls is None:
        assert line == "compact-gate: continue (no calls_ahead)"
    else:
        assert row["decision"] == "advise"


@pytest.mark.parametrize(
    ("artifact", "mode"),
    [
        pytest.param("contract", stat.S_IFLNK, id="contract-symlink"),
        pytest.param("contract", stat.S_IFIFO, id="contract-fifo"),
        pytest.param("owner", stat.S_IFLNK, id="owner-symlink"),
        pytest.param("owner", stat.S_IFIFO, id="owner-fifo"),
    ],
)
def test_nonregular_artifacts_never_opened(
    gate_state: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    artifact: str,
    mode: int,
) -> None:
    """Regular-only checks reject symlink and FIFO stat records before attempting any read."""
    home, _ = gate_state
    target = gate.CONTRACT_PATH if artifact == "contract" else gate.CONTRACT_PATH.with_suffix(".owner")
    original_lstat, original_open = Path.lstat, Path.open

    def unsafe_lstat(path: Path) -> Any:
        """Expose an unsafe named artifact independently of host symlink or FIFO privileges."""
        return SimpleNamespace(st_mode=mode, st_size=1) if path == target else original_lstat(path)

    def checked_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        """Ensure the unsafe artifact never reaches the potentially blocking open call."""
        assert path != target
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", unsafe_lstat)
    monkeypatch.setattr(Path, "open", checked_open)
    row, line = _run_gate(home, capsys)
    reason = "no contract" if artifact == "contract" else "unverified contract"
    assert row["decision"] == "continue"
    assert line == f"compact-gate: continue ({reason})"


@pytest.mark.parametrize("artifact", ["contract", "owner"])
def test_oversized_artifacts_fail_open(
    gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str], artifact: str
) -> None:
    """Files above the named contract and owner caps cannot supply a compaction decision."""
    home, _ = gate_state
    if artifact == "contract":
        _write_contract(b"x" * (gate.CONTRACT_MAX_BYTES + 1))
    else:
        gate.CONTRACT_PATH.with_suffix(".owner").write_bytes(b"x" * (gate.OWNER_MAX_BYTES + 1))
    row, line = _run_gate(home, capsys)
    reason = "no contract" if artifact == "contract" else "unverified contract"
    assert row["decision"] == "continue"
    assert line == f"compact-gate: continue ({reason})"


@pytest.mark.parametrize("owner", ["[]", "null", "1", '{"sha256": "invalid", "session_id": "sample-session"}'])
def test_invalid_owner_shapes_fail_open(
    gate_state: tuple[Path, Path], capsys: pytest.CaptureFixture[str], owner: str
) -> None:
    """Valid JSON without an intact ownership record still fails open without compaction."""
    home, _ = gate_state
    gate.CONTRACT_PATH.with_suffix(".owner").write_bytes(owner.encode())
    row, line = _run_gate(home, capsys)
    assert row["decision"] == "continue"
    assert line == "compact-gate: continue (unverified contract)"


@pytest.mark.parametrize("change", ["fifo", "symlink", "identity", "growth"])
def test_artifact_changed_during_open_rejected(
    gate_state: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: str,
) -> None:
    """A raced special file, replacement, or growth is rejected before bytes are trusted."""
    home, _ = gate_state
    original_fstat = gate.os.fstat
    closed = []
    original_close = gate.os.close

    def raced_fstat(descriptor: int) -> Any:
        """Alter the opened descriptor's stat while retaining the ordinary named path."""
        info = original_fstat(descriptor)
        return SimpleNamespace(
            st_mode={"fifo": stat.S_IFIFO, "symlink": stat.S_IFLNK}.get(change, info.st_mode),
            st_size=gate.CONTRACT_MAX_BYTES + 1 if change == "growth" else info.st_size,
            st_dev=info.st_dev,
            st_ino=info.st_ino + (change == "identity"),
        )

    def checked_close(descriptor: int) -> None:
        """Record that the unsafe descriptor is closed even after a rejected read."""
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(gate.os, "fstat", raced_fstat)
    monkeypatch.setattr(gate.os, "close", checked_close)
    row, line = _run_gate(home, capsys)
    assert row["decision"] == "continue"
    assert line == "compact-gate: continue (no contract)"
    assert len(closed) == 1


def test_read_cap_checks_post_stat_growth(
    gate_state: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A contract that grows after descriptor inspection is read only through cap plus one."""
    home, _ = gate_state
    original_fdopen = gate.os.fdopen

    def growing_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        """Expand the real file only after regular-file and size checks have passed."""
        gate.CONTRACT_PATH.write_bytes(b"x" * (gate.CONTRACT_MAX_BYTES + 1))
        return original_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(gate.os, "fdopen", growing_fdopen)
    row, line = _run_gate(home, capsys)
    assert row["decision"] == "continue"
    assert line == "compact-gate: continue (no contract)"


def test_binary_open_preserves_raw_contract_bytes(
    gate_state: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Windows binary-mode flags preserve digest identity across CRLF and Ctrl-Z bytes."""
    home, _ = gate_state
    _write_contract(b"- preserve: retained\x1a prose\r\n- gate-metadata:\r\n    - calls_ahead: 60\r\n")
    original_open = gate.os.open
    original_binary_flag = getattr(gate.os, "O_BINARY", 0)
    binary_flag = 1 << 28
    monkeypatch.setattr(gate.os, "O_BINARY", binary_flag, raising=False)
    observed = []

    def binary_open(path: Path, flags: int) -> int:
        """Require the simulated Windows binary flag while using native file access."""
        assert flags & binary_flag
        observed.append(path)
        return original_open(path, (flags & ~binary_flag) | original_binary_flag)

    monkeypatch.setattr(gate.os, "open", binary_open)
    row, _ = _run_gate(home, capsys)
    assert row["decision"] == "advise"
    assert observed == [gate.CONTRACT_PATH, gate.CONTRACT_PATH.with_suffix(".owner")]
