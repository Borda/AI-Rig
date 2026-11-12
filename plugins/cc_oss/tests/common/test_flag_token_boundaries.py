"""Prove flags cannot be enabled by quoted data or redirected contract state."""

import tempfile
from pathlib import Path

import parse_skill_flags as flags
import pytest


def _supports_symlinks() -> bool:
    """Probe directory-link support without assuming a particular operating system."""
    with tempfile.TemporaryDirectory() as folder:
        try:
            Path(folder, "link").symlink_to(folder, target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
    return True


_requires_symlink = pytest.mark.skipif(not _supports_symlinks(), reason="host cannot create directory symlinks")


@pytest.mark.parametrize(
    ("blob", "enabled", "clean"),
    [
        pytest.param("\t--APPROVE\n", "true", "", id="case-and-whitespace"),
        pytest.param('"example --approve text"', "false", '"example --approve text"', id="quoted-description"),
        pytest.param('"--approve"', "false", '"--approve"', id="quoted-flag"),
        pytest.param('--note="example --approve text"', "false", '--note="example --approve text"', id="attached-data"),
        pytest.param('"--keep notes --approve"', "false", '"--keep notes --approve"', id="quoted-keep"),
        pytest.param("it's\t--APPROVE", "true", "it's", id="prose-apostrophe"),
        pytest.param('--keep "notes --approve"', "false", "", id="keep-value"),
        pytest.param("-- --approve", "false", "-- --approve", id="end-of-options"),
        pytest.param("--agent -- --approve", "false", "--agent -- --approve", id="separator-is-not-a-value"),
        pytest.param('--keep foo" --approve "bar', "false", 'foo" --approve "bar', id="mixed-quoted-keep"),
    ],
)
def test_only_unquoted_option_tokens_enable_approval(blob: str, enabled: str, clean: str) -> None:
    """Approval requires an actual option token, never a fragment of a quoted value."""
    parsed = flags.parse_skill_flags(blob, ["approve"], ["agent"])
    assert parsed["FLAG_APPROVE"] == enabled
    assert parsed["CLEAN_ARGS"] == clean


def test_case_insensitive_value_flags_consume_boolean_shaped_values() -> None:
    """A value that resembles a boolean option stays data and preserves its case."""
    parsed = flags.parse_skill_flags('--AGENT="Team --approve"\t--KEEP "Keep Me"', ["approve"], ["agent"])
    assert parsed == {
        "FLAG_APPROVE": "false",
        "VALUE_AGENT": "Team --approve",
        "KEEP_ITEMS": "Keep Me",
        "CLEAN_ARGS": "",
    }


@_requires_symlink
@pytest.mark.parametrize("parent", [".temp", ".temp/state"])
def test_linked_state_directory_preserves_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A linked state ancestor must be refused before owner reads, unlinking or sentinel writes."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    target = outside / "state" if parent == ".temp" else outside
    target.mkdir(parents=True)
    contract = target / "skill-contract.md"
    contract.write_text("preserve", encoding="utf-8")
    link = project / parent
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("CSID", "boundary")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert flags.main(["--flags", "approve", "--keep-state", "foundry-setup", "--", "--approve"]) == 1
    assert contract.read_text(encoding="utf-8") == "preserve"
    assert not (tmp_path / "foundry-setup-approve-boundary").exists()
    assert "state" in capsys.readouterr().err


def test_out_of_project_contract_directory_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolved containment is checked even when a directory is not reported as a symlink."""
    project = tmp_path / "project"
    project.mkdir()
    contract = tmp_path / "skill-contract.md"
    contract.write_text("preserve", encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setenv("CSID", "boundary")
    monkeypatch.setattr(flags, "_CONTRACT", contract)
    assert flags.main(["--flags", "approve", "--keep-state", "foundry-setup", "--", ""]) == 1
    assert contract.read_text(encoding="utf-8") == "preserve"
