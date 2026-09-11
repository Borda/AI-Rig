"""Tests for ``bin/parse_kaggle_args.py`` — kaggle competition name and mode flags.

Covers:
* Competition name from the first token, including a leading-dash argument string
* Mode-flag precedence: inference forces offline, EDA overrides offline
* ``--type``, ``--resume``, ``--keep`` extraction — the flags the zsh ``BASH_REMATCH`` twin dropped
* Sentinel names, values, trailing newline, stale-contract clearing, and ``.experiments/kaggle/``
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "parse_kaggle_args.py"
_spec = importlib.util.spec_from_file_location("research_parse_kaggle_args", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

parse_modes = _mod.parse_modes
main = _mod.main


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run inside an isolated CWD — the script writes CWD-relative state, as the shell twin did."""
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("TMPDIR", str(sentinels))
    monkeypatch.setenv("CSID", "testsess")
    return tmp_path


def _sentinel(project_dir: Path, name: str) -> str:
    return (project_dir / "sentinels" / f"kaggle-{name}-testsess").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ("comp", (False, False, False)),
        ("comp --eda-only", (True, False, False)),
        ("comp --eda-only --offline-setup", (True, False, False)),
        ("comp --inference-only", (False, True, True)),
        ("comp --offline-setup", (False, False, True)),
        ("comp --eda-only --inference-only", (True, True, False)),
    ],
    ids=["plain", "eda", "eda-overrides-offline", "inference-forces-offline", "offline", "conflicting"],
)
def test_parse_modes(arguments: str, expected: tuple[bool, bool, bool]) -> None:
    assert parse_modes(arguments) == expected


def test_sentinels_and_dirs(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--", 'titanic --type tabular --resume old.py --keep "plan, decisions"']) == 0
    assert _sentinel(project, "competition-name") == "titanic\n"
    assert _sentinel(project, "eda-only") == "false\n"
    assert _sentinel(project, "inference-only") == "false\n"
    assert _sentinel(project, "offline-setup") == "false\n"
    assert _sentinel(project, "keep-items") == "plan, decisions\n"
    assert (project / "work" / ".experiments" / "kaggle").is_dir()
    out = capsys.readouterr().out
    assert "Competition: titanic" in out
    assert "Type: tabular" in out
    assert "Resume: old.py" in out


def test_absent_flags_report_placeholders(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--", "titanic"]) == 0
    out = capsys.readouterr().out
    assert "Type: auto-detect" in out
    assert "Resume: none" in out
    assert _sentinel(project, "keep-items") == "\n"


def test_stale_contract_is_cleared(project: Path) -> None:
    contract = project / "work" / ".temp" / "state" / "skill-contract.md"
    contract.parent.mkdir(parents=True)
    contract.write_text("stale", encoding="utf-8")
    assert main(["--", "titanic"]) == 0
    assert not contract.exists()


def test_missing_contract_is_not_an_error(project: Path) -> None:
    assert main(["--", "titanic"]) == 0


def test_leading_dash_arguments_do_not_break_parsing(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``$ARGUMENTS`` may start with a flag; the ``--`` separator must absorb it."""
    assert main(["--", "--eda-only"]) == 0
    assert "Competition: --eda-only" in capsys.readouterr().out
    assert _sentinel(project, "eda-only") == "true\n"


def test_empty_arguments(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--", ""]) == 0
    assert "Competition: \n" in capsys.readouterr().out
    assert _sentinel(project, "competition-name") == "\n"


def test_keep_flag_tolerates_multiple_spaces(project: Path) -> None:
    assert main(["--", 'comp --keep  "a, b"']) == 0
    assert _sentinel(project, "keep-items") == "a, b\n"


def test_unwritable_sentinel_dir_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "does" / "not" / "exist"))
    monkeypatch.setenv("CSID", "testsess")
    assert main(["--", "comp"]) == 1
