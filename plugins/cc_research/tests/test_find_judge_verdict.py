"""Tests for ``bin/find_judge_verdict.py`` — the fortify approved-baseline gate.

Covers:
* No judge report, missing ``Program:`` field, program mismatch, program missing on disk — all BLOCKED
* Newest-report selection by mtime
* Field extraction: bold markers stripped, internal spaces kept, trailing whitespace trimmed
* Sentinel values and the trailing newline the ``read -r`` reload contract needs
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "find_judge_verdict.py"
_spec = importlib.util.spec_from_file_location("research_find_judge_verdict", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

extract_field = _mod.extract_field
newest_verdict_file = _mod.newest_verdict_file
state_program_file = _mod.state_program_file
main = _mod.main


@pytest.fixture()
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


def _write_state(work: Path, run_id: str, program_file: str) -> None:
    state = work / "state" / run_id
    state.mkdir(parents=True)
    state.joinpath("state.json").write_text(json.dumps({"program_file": program_file}), encoding="utf-8")


def _args(work: Path, run_id: str = "r1") -> list[str]:
    return ["--state-dir-base", str(work / "state"), "--run-id", run_id, "--reports-dir", str(work / "reports")]


def test_blocked_without_any_verdict_file(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work = env / "work"
    assert main(_args(work)) == 1
    out = capsys.readouterr().out
    assert "fortify: BLOCKED — no judge verdict found in .reports/research/." in out
    assert "Ablation studies require an approved baseline. Run: /research:judge <program.md>" in out


def test_blocked_without_program_field(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work = env / "work"
    (work / "reports" / "judge-main-2026-01-01.md").write_text("**Verdict**: APPROVED\n", encoding="utf-8")
    assert main(_args(work)) == 1
    assert "judge verdict missing Program: field" in capsys.readouterr().out


def test_blocked_on_program_mismatch(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work = env / "work"
    work.joinpath("a.md").write_text("x", encoding="utf-8")
    work.joinpath("b.md").write_text("x", encoding="utf-8")
    (work / "reports" / "judge-main.md").write_text("Verdict: APPROVED\nProgram: a.md\n", encoding="utf-8")
    _write_state(work, "r1", "b.md")
    assert main(_args(work)) == 1
    out = capsys.readouterr().out
    assert "! BLOCKED — judge verdict references program 'a.md' but current experiment is for 'b.md'" in out
    assert "Run: /research:judge b.md" in out


def test_blocked_when_program_missing_on_disk(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work = env / "work"
    (work / "reports" / "judge-main.md").write_text("Verdict: APPROVED\nProgram: gone.md\n", encoding="utf-8")
    _write_state(work, "r1", "gone.md")
    assert main(_args(work)) == 1
    assert "! BLOCKED — program file gone.md referenced by judge verdict not found on disk" in capsys.readouterr().out


def test_happy_path_writes_sentinels(env: Path) -> None:
    work = env / "work"
    work.joinpath("prog.md").write_text("x", encoding="utf-8")
    (work / "reports" / "judge-main.md").write_text("**Verdict**: APPROVED\n**Program**: prog.md\n", encoding="utf-8")
    _write_state(work, "r1", "prog.md")
    assert main(_args(work)) == 0
    sentinels = env / "sentinels"
    assert (sentinels / "fortify-judge-verdict-testsess").read_text(encoding="utf-8") == "APPROVED\n"
    assert (sentinels / "fortify-program-file-testsess").read_text(encoding="utf-8") == "prog.md\n"


def test_relative_and_absolute_program_paths_match(env: Path) -> None:
    """The judge report may be relative where state.json is absolute; a raw compare would false-BLOCK."""
    work = env / "work"
    work.joinpath("prog.md").write_text("x", encoding="utf-8")
    (work / "reports" / "judge-main.md").write_text("Verdict: APPROVED\nProgram: prog.md\n", encoding="utf-8")
    _write_state(work, "r1", str(work / "prog.md"))
    assert main(_args(work)) == 0


def test_empty_state_program_does_not_block(env: Path) -> None:
    work = env / "work"
    work.joinpath("prog.md").write_text("x", encoding="utf-8")
    (work / "reports" / "judge-main.md").write_text("Verdict: APPROVED\nProgram: prog.md\n", encoding="utf-8")
    assert main(_args(work)) == 0


def test_newest_report_wins(env: Path) -> None:
    work = env / "work"
    reports = work / "reports"
    old = reports / "judge-old.md"
    new = reports / "judge-new.md"
    work.joinpath("prog.md").write_text("x", encoding="utf-8")
    old.write_text("Verdict: REJECTED\nProgram: prog.md\n", encoding="utf-8")
    new.write_text("Verdict: APPROVED\nProgram: prog.md\n", encoding="utf-8")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    assert newest_verdict_file(reports) == new
    assert main(_args(work)) == 0
    assert (env / "sentinels" / "fortify-judge-verdict-testsess").read_text(encoding="utf-8") == "APPROVED\n"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("**Verdict**: APPROVED", "APPROVED"),
        ("Verdict: NEEDS REVISION  ", "NEEDS REVISION"),
        ("verdict: approved", "approved"),
        ("## Verdict", ""),
    ],
    ids=["bold", "internal-spaces-kept", "lowercase", "no-colon"],
)
def test_extract_verdict(line: str, expected: str) -> None:
    assert extract_field([line], _mod._VERDICT_LINE_RE, _mod._VERDICT_STRIP_RE) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Program: prog.md", "prog.md"),
        ("**Program_file**: sub/prog.md", "sub/prog.md"),
        ("Program file:   spaced.md  ", "spaced.md"),
    ],
    ids=["plain", "program_file", "program-file-spaced"],
)
def test_extract_program(line: str, expected: str) -> None:
    assert extract_field([line], _mod._PROGRAM_LINE_RE, _mod._PROGRAM_STRIP_RE) == expected


@pytest.mark.parametrize("payload", ["not json", '{"program_file": null}', "{}"])
def test_state_program_file_degrades_to_empty(tmp_path: Path, payload: str) -> None:
    state = tmp_path / "state.json"
    state.write_text(payload, encoding="utf-8")
    assert state_program_file(state) == ""


def test_state_program_file_missing(tmp_path: Path) -> None:
    assert state_program_file(tmp_path / "absent.json") == ""
