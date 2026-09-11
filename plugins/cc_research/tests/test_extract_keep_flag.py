"""Tests for ``bin/extract-keep-flag.py`` — keep-items and optional venue extraction.

Covers:
* Keep-value extraction (the zsh ``BASH_REMATCH`` twin dropped this silently), multi-space form
* Stale compaction-contract clearing, and that a missing contract is not an error
* Missing slug / missing session id → exit 2
* ``--venue-choices``: valid, absent, and invalid values, and that keep state is already
  persisted when an invalid venue exits 2
* Argv holding a payload that starts with ``--`` — the reason this script avoids argparse
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "extract-keep-flag.py"
_spec = importlib.util.spec_from_file_location("research_extract_keep_flag", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

option_value = _mod.option_value
main = _mod.main

_VENUES = "--venue-choices"
_CHOICES = "CVPR,NeurIPS,ICML,workshop"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated CWD (the contract path is CWD-relative) plus a private sentinel dir."""
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("TMPDIR", str(sentinels))
    monkeypatch.setenv("CSID", "testsess")
    return tmp_path


def _keep(env_dir: Path) -> str:
    return (env_dir / "sentinels" / "fortify-keep-items-testsess").read_text(encoding="utf-8")


def _venue(env_dir: Path) -> str:
    return (env_dir / "sentinels" / "fortify-venue-testsess").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([_VENUES, "CVPR,ICML"], "CVPR,ICML"),
        ([f"{_VENUES}=CVPR"], "CVPR"),
        (["--other", "x"], ""),
        ([_VENUES], ""),
    ],
    ids=["spaced", "attached", "absent", "dangling"],
)
def test_option_value(argv: list[str], expected: str) -> None:
    assert option_value(argv, _VENUES) == expected


def test_missing_slug_exits_two(env: Path) -> None:
    assert main(["extract-keep-flag.py"]) == 2


def test_missing_session_id_exits_two(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CSID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert main(["extract-keep-flag.py", "fortify", "x"]) == 2


def test_keep_value_persisted(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract-keep-flag.py", "fortify", 'run --keep "plan, decisions"']) == 0
    assert _keep(env) == "plan, decisions\n"
    assert capsys.readouterr().out == "plan, decisions\n"


def test_absent_keep_writes_empty(env: Path) -> None:
    assert main(["extract-keep-flag.py", "fortify", "run"]) == 0
    assert _keep(env) == "\n"


def test_stale_contract_cleared(env: Path) -> None:
    contract = env / "work" / ".temp" / "state" / "skill-contract.md"
    contract.parent.mkdir(parents=True)
    contract.write_text("stale", encoding="utf-8")
    assert main(["extract-keep-flag.py", "fortify", "run"]) == 0
    assert not contract.exists()


def test_payload_starting_with_dash(env: Path) -> None:
    """Five call sites pass ``"$ARGUMENTS"`` positionally and it often starts with a flag."""
    assert main(["extract-keep-flag.py", "fortify", '--venue CVPR --keep "a"', _VENUES, _CHOICES]) == 0
    assert _keep(env) == "a\n"
    assert _venue(env) == "CVPR\n"


@pytest.mark.parametrize("venue", ["CVPR", "NeurIPS", "ICML", "workshop"])
def test_valid_venues(env: Path, venue: str) -> None:
    assert main(["extract-keep-flag.py", "fortify", f"run --venue {venue}", _VENUES, _CHOICES]) == 0
    assert _venue(env) == f"{venue}\n"


def test_absent_venue_writes_empty(env: Path) -> None:
    """Empty venue is legal — it is what makes the F6 skip rule fire; there is no default venue."""
    assert main(["extract-keep-flag.py", "fortify", "run", _VENUES, _CHOICES]) == 0
    assert _venue(env) == "\n"


def test_invalid_venue_exits_two(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract-keep-flag.py", "fortify", "run --venue SIGGRAPH", _VENUES, _CHOICES]) == 2
    err = capsys.readouterr().err
    assert "fortify: invalid --venue 'SIGGRAPH' — valid: CVPR, NeurIPS, ICML, workshop" in err


def test_invalid_venue_still_leaves_keep_state_written(env: Path) -> None:
    """Contract clearing and the keep sentinel happen first, exactly as the shell twin ordered them."""
    assert main(["extract-keep-flag.py", "fortify", 'run --venue SIGGRAPH --keep "a"', _VENUES, _CHOICES]) == 2
    assert _keep(env) == "a\n"
    assert not (env / "sentinels" / "fortify-venue-testsess").exists()


def test_no_venue_sentinel_without_choices(env: Path) -> None:
    """Callers that never pass --venue-choices keep the original single-sentinel behaviour."""
    assert main(["extract-keep-flag.py", "sweep", "run --venue CVPR"]) == 0
    assert not (env / "sentinels" / "sweep-venue-testsess").exists()
