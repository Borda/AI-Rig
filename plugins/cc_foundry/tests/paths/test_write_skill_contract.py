"""Tests for write_skill_contract bin script.

Covers the contract file's exact shape, argument validation, and the fact that the file is written relative to the
caller's working directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import write_skill_contract as wsc

_ARGS = ["foundry:distill", "gap-analysis", ".reports/x", "run-dir=n/a", "Step 3 → Step 4"]


class TestContractFile:
    """Covers the written contract."""

    def test_writes_all_five_fields(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each argument lands on its own labelled line, in order."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main(_ARGS) == 0
        text = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert text.splitlines() == [
            "## Active Skill Contract",
            "- skill: foundry:distill · phase: gap-analysis",
            "- run-dir: .reports/x",
            "- preserve: run-dir=n/a",
            "- next: Step 3 → Step 4",
        ]

    def test_labelled_list_appended(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The 7-argument form appends the ledger a `next:` string refers to.

        Without it a post-compaction resume is told to skip candidates "above" that were never written, and re-probes
        what it already ruled out.
        """
        monkeypatch.chdir(tmp_path)
        assert (
            wsc.main([*_ARGS, "probed (do NOT re-probe)", "hypothesis A :: Ruled-out\nhypothesis B :: Confirmed"]) == 0
        )
        text = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert text.splitlines()[-3:] == [
            "- probed (do NOT re-probe):",
            "    - hypothesis A :: Ruled-out",
            "    - hypothesis B :: Confirmed",
        ]

    def test_empty_list_omits_block(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty ledger produces no label line, so callers need not branch."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main([*_ARGS, "tried", ""]) == 0
        text = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert "tried" not in text
        assert text.splitlines()[-1].startswith("- next:")

    def test_blank_list_lines_dropped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A trailing newline from a `head`/`tail` capture does not emit an empty bullet."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main([*_ARGS, "tried", "only one\n\n"]) == 0
        text = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert text.splitlines()[-2:] == ["- tried:", "    - only one"]

    def test_creates_parent_directories(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """.temp/state/ is created when absent."""
        monkeypatch.chdir(tmp_path)
        wsc.main(_ARGS)
        assert (tmp_path / ".temp" / "state").is_dir()

    def test_overwrites_previous_contract(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A later boundary replaces the earlier contract rather than appending."""
        monkeypatch.chdir(tmp_path)
        wsc.main(_ARGS)
        wsc.main(["foundry:audit", "step-6", "n/a", "p", "next"])
        text = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert "foundry:distill" not in text
        assert "foundry:audit" in text

    def test_written_relative_to_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The contract belongs to the project the skill runs in, not the script's location."""
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.chdir(project)
        wsc.main(_ARGS)
        assert (project / ".temp" / "state" / "skill-contract.md").is_file()
        assert not (tmp_path / ".temp").exists()

    def test_placeholder_tokens_survive_verbatim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`<REFINE_ITER>`-style fill-in tokens are written through untouched."""
        monkeypatch.chdir(tmp_path)
        wsc.main(["s", "iter <REFINE_ITER>/<MAX>", "n/a", "p", "n"])
        text = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert "iter <REFINE_ITER>/<MAX>" in text


class TestArgumentValidation:
    """Covers the argument contract."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="no-args"),
            pytest.param(["a", "b", "c", "d"], id="four-args"),
            pytest.param(["a", "b", "c", "d", "e", "f"], id="six-args"),
        ],
    )
    def test_wrong_arity_exits_two(self, argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Anything but five or seven arguments is a usage error."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main(argv) == 2

    def test_empty_skill_exits_two(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty skill name would produce a contract no hook could attribute."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main(["", "phase", "dir", "preserve", "next"]) == 2

    def test_help_exits_zero_with_usage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--help prints usage and exits 0, per the bin/ help contract."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            wsc.main(["--help"])
        assert exc.value.code == 0
        assert "usage:" in capsys.readouterr().out

    def test_no_file_written_on_usage_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rejected invocation leaves no partial contract behind."""
        monkeypatch.chdir(tmp_path)
        wsc.main(["only-one"])
        assert not (tmp_path / ".temp").exists()
