"""Tests for check_mode_dispatch.py — dangling Mode: dispatch detection."""

from __future__ import annotations

from pathlib import Path

import pytest

# conftest.py registers bin/ scripts as importable modules
from check_mode_dispatch import Finding, check_file, extract_mode_headers, extract_mode_refs, find_skill_files, main

_CLEAN_SKILL = """\
# distill

**If first token equals `memory`**: skip Steps 2-5 and go to "Mode: Memory Distillation" below.

## Mode: Memory Distillation

Body of the memory distillation mode.
"""

_DANGLING_SKILL = """\
# distill

**If first token equals `lessons`**: go to "Mode: Lessons Distillation" below.

## Mode: Memory Distillation

The header was renamed; the dispatch above still points at the old name.
"""

_QUALIFIER_SKILL = """\
# distill

**If first token equals `memory`**: go to "Mode: Memory Distillation" below.

## Mode: Memory Distillation — only when explicitly requested by the user

Body.
"""

_MULTI_MODE_SKILL = """\
# distill

Skip to **Mode: Executables Extraction** below.
skip to **Mode: Memory Pruning** below.
see **Mode: External Distillation** below.

## Mode: Executables Extraction
x

### Mode: Memory Pruning
y

## Mode: External Distillation
z
"""


def _write(tmp: Path, name: str, text: str) -> Path:
    """Write ``text`` to ``tmp/name`` and return the path."""
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return path


def _write_nested_skill(root: Path, text: str) -> Path:
    """Write ``text`` as ``root/foundry/skills/distill/SKILL.md`` (the layout --scan-dir discovers) and return it."""
    path = root / "foundry" / "skills" / "distill" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestExtractModeRefs:
    """Covers extraction of the mode names a skill dispatches to."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param('go to "Mode: Memory Distillation" below.', ["Memory Distillation"], id="quoted-go-to"),
            pytest.param(
                "Skip to **Mode: Executables Extraction** below.",
                ["Executables Extraction"],
                id="bold-skip-to-any-case",
            ),
            pytest.param("see **Mode: External Distillation** below.", ["External Distillation"], id="see-form"),
            pytest.param(
                'see **Mode: A** ... go to "Mode: B" ... go to "Mode: A"', ["A", "B"], id="duplicates-collapse-in-order"
            ),
            pytest.param("The word Mode: appears but no verb dispatches it.", [], id="no-dispatch-verb"),
        ],
    )
    def test_extracts_dispatched_mode_names(self, text: str, expected: list[str]) -> None:
        """Mode names introduced by a dispatch verb are returned, first-seen order, without repeats.

        Covers a quoted ``go to``, a bold ``Skip to`` whose verb capitalization does not matter, a ``see`` instruction,
        repeated references collapsing to first-seen order, and text mentioning ``Mode:`` with no dispatching verb.
        """
        assert extract_mode_refs(text) == expected


class TestExtractModeHeaders:
    """Covers extraction of the mode names a skill defines as headers."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("## Mode: A\n### Mode: B", {"A", "B"}, id="h2-and-h3"),
            pytest.param(
                "## Mode: Memory Distillation — only when x", {"Memory Distillation"}, id="trailing-qualifier-stripped"
            ),
            pytest.param(
                "## Mode: adversarial (alias: --challenge)", {"adversarial"}, id="trailing-parenthetical-stripped"
            ),
            pytest.param("# Mode: NotAHeader", set(), id="h1-is-not-a-header"),
        ],
    )
    def test_extracts_mode_header_names(self, text: str, expected: set[str]) -> None:
        """Level-2 and level-3 mode headers are captured by bare name.

        A trailing ``— qualifier`` or ``(alias: …)`` parenthetical is removed from the name, and a single-``#`` line is
        not a mode header.
        """
        assert extract_mode_headers(text) == expected


class TestCheckFile:
    """Covers the per-file dangling-reference verdict."""

    @pytest.mark.parametrize(
        "skill",
        [
            pytest.param(_CLEAN_SKILL, id="matching-header"),
            pytest.param(_QUALIFIER_SKILL, id="header-with-trailing-qualifier"),
            pytest.param(_MULTI_MODE_SKILL, id="several-dispatch-forms"),
        ],
    )
    def test_resolvable_reference_is_clean(self, tmp_path: Path, skill: str) -> None:
        """A mode reference with a matching header produces no finding.

        Covers a plain match, a header carrying a trailing ``— qualifier`` that still matches a bare reference, and
        several dispatch forms that each resolve to their own header.
        """
        assert check_file(_write(tmp_path, "SKILL.md", skill)) == []

    def test_dangling_reference_flagged(self, tmp_path: Path) -> None:
        """A reference whose header was renamed away is flagged.

        The skill still dispatches to "Lessons Distillation" while only "Memory Distillation" is defined.
        """
        findings = check_file(_write(tmp_path, "SKILL.md", _DANGLING_SKILL))

        assert len(findings) == 1
        assert findings[0].mode_name == "Lessons Distillation"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """An unreadable path yields no findings rather than raising."""
        assert check_file(tmp_path / "does-not-exist.md") == []


class TestFindSkillFiles:
    """Covers skill discovery under a scan directory."""

    def test_globs_skill_md_under_dir(self, tmp_path: Path) -> None:
        """Discover nested skill definitions beneath the scan directory, ignoring a ``SKILL.md`` at the wrong depth."""
        target = tmp_path / "foundry" / "skills" / "audit" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("# audit", encoding="utf-8")
        (tmp_path / "foundry" / "SKILL.md").write_text("x", encoding="utf-8")

        assert find_skill_files(tmp_path) == [target]


class TestFinding:
    """Covers the finding's rendered message."""

    def test_message_format(self) -> None:
        """The message matches the documented MODE-DISPATCH: line."""
        msg = Finding("plugins/cc_foundry/skills/distill/SKILL.md", "Lessons Distillation").message

        assert msg == (
            "MODE-DISPATCH: plugins/cc_foundry/skills/distill/SKILL.md: "
            'references "Mode: Lessons Distillation" with no matching header'
        )


class TestMain:
    """Covers the command-line contract and exit codes."""

    def test_clean_files_exit_0(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Explicit clean files exit 0 with a pass line."""
        exit_code = main([str(_write(tmp_path, "SKILL.md", _CLEAN_SKILL))])

        assert exit_code == 0
        assert "✓" in capsys.readouterr().out

    def test_dangling_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Report a dangling mode reference as a failure naming the missing mode."""
        exit_code = main([str(_write(tmp_path, "SKILL.md", _DANGLING_SKILL))])

        assert exit_code == 1
        out = capsys.readouterr().out
        assert "MODE-DISPATCH:" in out
        assert "Lessons Distillation" in out

    def test_scan_dir_finds_nested_skills(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Discover and validate nested skills from an explicit scan directory."""
        _write_nested_skill(tmp_path, _DANGLING_SKILL)

        exit_code = main(["--scan-dir", str(tmp_path)])

        assert exit_code == 1
        assert "MODE-DISPATCH:" in capsys.readouterr().out

    def test_non_directory_scan_dir_exits_2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A non-directory ``--scan-dir`` is an argument error (exit 2)."""
        exit_code = main(["--scan-dir", str(tmp_path / "nope")])

        assert exit_code == 2
        assert "not a directory" in capsys.readouterr().err

    def test_no_targets_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No files and no scan dir is an argument error (exit 2)."""
        exit_code = main([])

        assert exit_code == 2
        assert "no files to check" in capsys.readouterr().err
