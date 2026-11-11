"""Tests for ``bin/find_judge_verdict.py`` — the fortify approved-baseline gate.

Covers:
* No judge report, missing ``Program:`` field, program mismatch, program missing on disk — all BLOCKED
* Newest-report selection by mtime
* Field extraction: bold markers stripped, internal spaces kept, trailing whitespace trimmed
* Sentinel values and the trailing newline the ``read -r`` reload contract needs
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from _research_text import load_module

_mod = load_module(Path(__file__).resolve().parents[2] / "bin" / "find_judge_verdict.py", "research_find_judge_verdict")

extract_field = _mod.extract_field
newest_verdict_file = _mod.newest_verdict_file
state_program_file = _mod.state_program_file
main = _mod.main


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated CWD plus sentinel dir under a fixed session token."""
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    work = tmp_path / "work"
    (work / "reports").mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.setenv("TMPDIR", str(sentinels))
    monkeypatch.setenv("CSID", "testsess")
    return tmp_path


@pytest.fixture
def work(env: Path) -> Path:
    """Working directory holding the ``reports/`` dir the gate scans."""
    return env / "work"


def _write_state(work: Path, run_id: str, program_file: str) -> None:
    """Record ``program_file`` in the state file of run ``run_id`` under ``work/state``."""
    state = work / "state" / run_id
    state.mkdir(parents=True)
    state.joinpath("state.json").write_text(json.dumps({"program_file": program_file}), encoding="utf-8")


def _write_report(work: Path, name: str, text: str) -> Path:
    """Write a judge report called ``name`` into ``work/reports`` and return its path."""
    program = extract_field(text.splitlines(), _mod._PROGRAM_LINE_RE, _mod._PROGRAM_STRIP_RE)
    if program and (work / program).is_file():
        text += "Program SHA256: " + hashlib.sha256((work / program).read_bytes()).hexdigest() + "\n"
    report = work / "reports" / name
    report.write_text(text, encoding="utf-8")
    return report


def _verdict_sentinel(env: Path) -> str:
    """Text of the judge-verdict sentinel the gate persists for the shell to reload."""
    return (env / "sentinels" / "fortify-judge-verdict-testsess").read_text(encoding="utf-8")


def _args(work: Path, run_id: str = "r1") -> list[str]:
    """CLI arguments pointing the gate at ``work``'s state dir, run id and reports dir."""
    return ["--state-dir-base", str(work / "state"), "--run-id", run_id, "--reports-dir", str(work / "reports")]


class TestGate:
    """The approved-baseline gate: BLOCKED outcomes and the passing path."""

    def test_blocked_without_any_verdict_file(self, work: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """With no judge report at all the gate blocks and tells the user which skill produces one."""
        assert main(_args(work)) == 1

        out = capsys.readouterr().out
        assert "fortify: BLOCKED — no judge verdict found in .reports/research/." in out
        assert "Ablation studies require an approved baseline. Run: /research:judge <program.md>" in out

    def test_blocked_without_program_field(self, work: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A verdict that names no program cannot be tied to this experiment, so the gate blocks."""
        _write_report(work, "judge-main-2026-01-01.md", "**Verdict**: APPROVED\n")

        assert main(_args(work)) == 1
        assert "judge verdict missing Program: field" in capsys.readouterr().out

    def test_blocked_on_program_mismatch(self, work: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A verdict approving program ``a.md`` does not unlock an experiment on ``b.md``.

        Approval is per program; the message names both so the user knows which judge run to repeat.
        """
        work.joinpath("a.md").write_text("x", encoding="utf-8")
        work.joinpath("b.md").write_text("x", encoding="utf-8")
        _write_report(work, "judge-main.md", "Verdict: APPROVED\nProgram: a.md\n")
        _write_state(work, "r1", "b.md")

        assert main(_args(work)) == 1

        out = capsys.readouterr().out
        assert "! BLOCKED — judge verdict references program 'a.md' but current experiment is for 'b.md'" in out
        assert "Run: /research:judge b.md" in out

    def test_blocked_when_program_missing_on_disk(self, work: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A verdict whose program file no longer exists on disk is not a usable baseline."""
        _write_report(work, "judge-main.md", "Verdict: APPROVED\nProgram: gone.md\n")
        _write_state(work, "r1", "gone.md")

        assert main(_args(work)) == 1
        assert "! BLOCKED — program file gone.md referenced by judge verdict not found on disk" in (
            capsys.readouterr().out
        )

    def test_empty_state_program_blocks(self, work: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """No program_file to cross-match → BLOCKED, else any program's newest APPROVED verdict would pass the gate."""
        work.joinpath("prog.md").write_text("x", encoding="utf-8")
        _write_report(work, "judge-main.md", "Verdict: APPROVED\nProgram: prog.md\n")

        assert main(_args(work)) == 1
        assert "source run records no program_file" in capsys.readouterr().out

    def test_happy_path_writes_sentinels(self, env: Path, work: Path) -> None:
        """An approved verdict for the matching program exits 0 and persists verdict and program for the shell.

        Both sentinels end in a newline: the shell reloads them with ``read -r``.
        """
        work.joinpath("prog.md").write_text("x", encoding="utf-8")
        _write_report(work, "judge-main.md", "**Verdict**: APPROVED\n**Program**: prog.md\n")
        _write_state(work, "r1", "prog.md")

        assert main(_args(work)) == 0

        assert _verdict_sentinel(env) == "APPROVED\n"
        assert (env / "sentinels" / "fortify-program-file-testsess").read_text(encoding="utf-8") == "prog.md\n"

    def test_relative_and_absolute_program_paths_match(self, work: Path) -> None:
        """The judge report may be relative where state.json is absolute; a raw compare would false-BLOCK."""
        work.joinpath("prog.md").write_text("x", encoding="utf-8")
        _write_report(work, "judge-main.md", "Verdict: APPROVED\nProgram: prog.md\n")
        _write_state(work, "r1", str(work / "prog.md"))

        assert main(_args(work)) == 0

    def test_newest_report_wins(self, env: Path, work: Path) -> None:
        """When several judge reports exist the newest by mtime decides the verdict.

        An older REJECTED report must not override a newer APPROVED one (nor the reverse), so mtimes are set explicitly.
        """
        work.joinpath("prog.md").write_text("x", encoding="utf-8")
        old = _write_report(work, "judge-old.md", "Verdict: REJECTED\nProgram: prog.md\n")
        new = _write_report(work, "judge-new.md", "Verdict: APPROVED\nProgram: prog.md\n")
        _write_state(work, "r1", str(work / "prog.md"))
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))

        assert newest_verdict_file(work / "reports") == new
        assert main(_args(work)) == 0
        assert _verdict_sentinel(env) == "APPROVED\n"


class TestExtractField:
    """Field extraction from a judge report line."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param("**Verdict**: APPROVED", "APPROVED", id="bold"),
            pytest.param("Verdict: NEEDS REVISION  ", "NEEDS REVISION", id="internal-spaces-kept"),
            pytest.param("verdict: approved", "approved", id="lowercase"),
            pytest.param("## Verdict", "", id="no-colon"),
        ],
    )
    def test_verdict(self, line: str, expected: str) -> None:
        """Bold markers are stripped, internal spaces kept, trailing whitespace trimmed; a heading yields nothing."""
        assert extract_field([line], _mod._VERDICT_LINE_RE, _mod._VERDICT_STRIP_RE) == expected

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param("Program: prog.md", "prog.md", id="plain"),
            pytest.param("**Program_file**: sub/prog.md", "sub/prog.md", id="program_file"),
            pytest.param("Program file:   spaced.md  ", "spaced.md", id="program-file-spaced"),
        ],
    )
    def test_program(self, line: str, expected: str) -> None:
        """The program path is read from ``Program``, ``Program_file`` or ``Program file`` lines, trimmed."""
        assert extract_field([line], _mod._PROGRAM_LINE_RE, _mod._PROGRAM_STRIP_RE) == expected


class TestStateProgramFile:
    """Reading ``program_file`` from a run's state file degrades to an empty string."""

    @pytest.mark.parametrize("payload", ["not json", '{"program_file": null}', "{}"])
    def test_unusable_state_degrades_to_empty(self, tmp_path: Path, payload: str) -> None:
        """Invalid JSON, a null program or a missing key all read as no program (empty string), never an error."""
        state = tmp_path / "state.json"
        state.write_text(payload, encoding="utf-8")

        assert state_program_file(state) == ""

    def test_missing_file_degrades_to_empty(self, tmp_path: Path) -> None:
        """A state file that does not exist reads as no program rather than raising."""
        assert state_program_file(tmp_path / "absent.json") == ""
