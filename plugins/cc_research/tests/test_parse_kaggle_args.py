"""Tests for ``bin/parse_kaggle_args.py`` — kaggle competition name and mode flags.

Covers:
* Competition name from the first token, rejecting empty or leading-flag argument strings
* Mode-flag precedence: inference forces offline, EDA overrides offline
* ``--type``, ``--resume``, ``--keep`` extraction — the flags the zsh ``BASH_REMATCH`` twin dropped
* Sentinel names, values, trailing newline, stale-contract clearing, and ``.experiments/kaggle/``
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _research_text import load_module

_mod = load_module(
    Path(__file__).resolve().parent.parent / "bin" / "parse_kaggle_args.py", "research_parse_kaggle_args"
)

parse_modes = _mod.parse_modes
main = _mod.main


@pytest.fixture
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
    """Return the content of the session-scoped ``kaggle-<name>`` sentinel written under the isolated TMPDIR."""
    return (project_dir / "sentinels" / f"kaggle-{name}-testsess").read_text(encoding="utf-8")


class TestParseModes:
    """Mode-flag precedence resolved from the raw ``$ARGUMENTS`` string."""

    @pytest.mark.parametrize(
        "arguments",
        ['comp "--eda-only"', 'comp --keep "--inference-only --offline-setup"', 'comp --resume "--eda-only.py"'],
    )
    def test_quoted_values_do_not_enable_modes(self, arguments: str) -> None:
        """Flag-shaped text in descriptions or option values cannot change notebook mode."""
        assert parse_modes(arguments) == (False, False, False)

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("comp", (False, False, False), id="plain"),
            pytest.param("comp --eda-only", (True, False, False), id="eda"),
            pytest.param("comp --eda-only --offline-setup", (True, False, False), id="eda-overrides-offline"),
            pytest.param("comp --inference-only", (False, True, True), id="inference-forces-offline"),
            pytest.param("comp --offline-setup", (False, False, True), id="offline"),
            pytest.param("comp --eda-only --inference-only", (True, True, False), id="conflicting"),
        ],
    )
    def test_resolves_flag_precedence(self, arguments: str, expected: tuple[bool, bool, bool]) -> None:
        """``(eda_only, inference_only, offline_setup)`` follows the precedence rules.

        Inference forces offline setup, EDA overrides offline, and a conflicting EDA plus inference pair keeps both
        flags without forcing offline.
        """
        assert parse_modes(arguments) == expected


class TestMain:
    """Sentinels, printed summary and side effects of ``main()``."""

    def test_conflict_recovery_retains_explicit_offline_flag(self, project: Path) -> None:
        """The conflict branch can restore the explicit flag without rereading raw shell text."""
        assert main(["--", "titanic --eda-only --inference-only --offline-setup"]) == 0
        assert _sentinel(project, "explicit-offline-setup") == "true\n"

    def test_quoted_offline_flag_is_not_explicit(self, project: Path) -> None:
        """Quoted prose cannot select offline setup during conflict recovery."""
        assert main(["--", 'titanic --eda-only --inference-only --keep "--offline-setup"']) == 0
        assert _sentinel(project, "explicit-offline-setup") == "false\n"

    def test_writes_sentinels_and_creates_dirs(self, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A full argument string yields one sentinel per field, the ``.experiments/kaggle`` dir and a summary.

        ``--type``, ``--resume`` and ``--keep`` are the flags the zsh ``BASH_REMATCH`` twin dropped; each must land in
        its own sentinel with a trailing newline.
        """
        code = main(["--", 'titanic --type tabular --resume old.py --keep "plan, decisions"'])

        out = capsys.readouterr().out
        assert code == 0
        assert _sentinel(project, "competition-name") == "titanic\n"
        assert _sentinel(project, "eda-only") == "false\n"
        assert _sentinel(project, "inference-only") == "false\n"
        assert _sentinel(project, "offline-setup") == "false\n"
        assert _sentinel(project, "resume") == "old.py\n"
        assert _sentinel(project, "keep-items") == "plan, decisions\n"
        assert (project / "work" / ".experiments" / "kaggle").is_dir()
        assert "Competition: titanic" in out
        assert "Type: tabular" in out
        assert "Resume: old.py" in out

    def test_absent_flags_report_placeholders(self, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """With only a competition name, the summary shows placeholders and the optional sentinels stay empty."""
        code = main(["--", "titanic"])

        out = capsys.readouterr().out
        assert code == 0
        assert "Type: auto-detect" in out
        assert "Resume: none" in out
        assert _sentinel(project, "resume") == "\n"
        assert _sentinel(project, "keep-items") == "\n"

    def test_clears_stale_contract(self, project: Path) -> None:
        """A leftover ``.temp/state/skill-contract.md`` from a crashed run is deleted at start.

        A stale contract would otherwise leak its preserve items into the next unrelated compaction.
        """
        contract = project / "work" / ".temp" / "state" / "skill-contract.md"
        contract.parent.mkdir(parents=True)
        contract.write_text("stale", encoding="utf-8")

        code = main(["--", "titanic"])

        assert code == 0
        assert not contract.exists()

    def test_missing_contract_is_not_an_error(self, project: Path) -> None:
        """A first run with no contract file to clear still exits 0."""
        assert main(["--", "titanic"]) == 0

    @pytest.mark.parametrize("arguments", ["--eda-only", ""])
    def test_edge_case_arguments_parse(
        self,
        project: Path,
        capsys: pytest.CaptureFixture[str],
        arguments: str,
    ) -> None:
        """A missing competition or flag in its place fails before creating state."""
        with pytest.raises(SystemExit) as exc:
            main(["--", arguments])

        assert exc.value.code == 2
        assert "usage:" in capsys.readouterr().err
        assert not (project / "work" / ".experiments").exists()
        assert not (project / "sentinels" / "kaggle-competition-name-testsess").exists()

    def test_keep_flag_tolerates_multiple_spaces(self, project: Path) -> None:
        """``--keep`` followed by several spaces before its quoted value still extracts the value."""
        code = main(["--", 'comp --keep  "a, b"'])

        assert code == 0
        assert _sentinel(project, "keep-items") == "a, b\n"

    def test_unwritable_sentinel_dir_exits_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A TMPDIR that does not exist makes the sentinel write fail, so the script exits 1 instead of crashing."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TMPDIR", str(tmp_path / "does" / "not" / "exist"))
        monkeypatch.setenv("CSID", "testsess")

        assert main(["--", "comp"]) == 1
