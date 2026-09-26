"""Tests for check_mode_dispatch.py — dangling Mode: dispatch detection."""

from __future__ import annotations

from pathlib import Path

import pytest

# conftest.py registers bin/ scripts as importable modules
from check_mode_dispatch import (
    Finding,
    check_file,
    extract_mode_headers,
    extract_mode_refs,
    find_skill_files,
    main,
)

_HAS_PROJECT_PLUGIN_TREE = (Path(__file__).resolve().parent.parent.parent.parent / "cc_foundry").is_dir()

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# extract_mode_refs
# ---------------------------------------------------------------------------


class TestExtractModeRefs:
    def test_quoted_go_to(self) -> None:
        """Extract a mode name from a quoted navigation instruction."""
        assert extract_mode_refs('go to "Mode: Memory Distillation" below.') == ["Memory Distillation"]

    def test_bold_skip_to(self) -> None:
        """Match a bold mode reference without regard to verb capitalization."""
        assert extract_mode_refs("Skip to **Mode: Executables Extraction** below.") == ["Executables Extraction"]

    def test_see_form(self) -> None:
        """Recognize a mode reference introduced by a see instruction."""
        assert extract_mode_refs("see **Mode: External Distillation** below.") == ["External Distillation"]

    def test_multiple_refs_deduplicated_in_order(self) -> None:
        """Repeated references collapse to first-seen order."""
        text = 'see **Mode: A** ... go to "Mode: B" ... go to "Mode: A"'
        assert extract_mode_refs(text) == ["A", "B"]

    def test_no_dispatch_returns_empty(self) -> None:
        """Text with no dispatch verb yields no references."""
        assert extract_mode_refs("The word Mode: appears but no verb dispatches it.") == []


# ---------------------------------------------------------------------------
# extract_mode_headers
# ---------------------------------------------------------------------------


class TestExtractModeHeaders:
    def test_h2_and_h3(self) -> None:
        """Both `##` and `###` headers are captured."""
        assert extract_mode_headers("## Mode: A\n### Mode: B") == {"A", "B"}

    def test_trailing_qualifier_stripped(self) -> None:
        """A trailing `— qualifier` is removed from the header name."""
        assert extract_mode_headers("## Mode: Memory Distillation — only when x") == {"Memory Distillation"}

    def test_trailing_parenthetical_stripped(self) -> None:
        """A trailing `(alias: …)` parenthetical is removed from the header name."""
        assert extract_mode_headers("## Mode: adversarial (alias: --challenge)") == {"adversarial"}

    def test_h1_not_a_mode_header(self) -> None:
        """A single-`#` line is not treated as a mode header."""
        assert extract_mode_headers("# Mode: NotAHeader") == set()


# ---------------------------------------------------------------------------
# check_file
# ---------------------------------------------------------------------------


class TestCheckFile:
    def test_clean_when_header_present(self, tmp_path: Path) -> None:
        """A reference with a matching header produces no finding."""
        path = _write(tmp_path, "SKILL.md", _CLEAN_SKILL)
        assert check_file(path) == []

    def test_dangling_reference_flagged(self, tmp_path: Path) -> None:
        """A reference whose header was renamed away is flagged."""
        path = _write(tmp_path, "SKILL.md", _DANGLING_SKILL)
        findings = check_file(path)
        assert len(findings) == 1
        assert findings[0].mode_name == "Lessons Distillation"

    def test_header_with_qualifier_is_clean(self, tmp_path: Path) -> None:
        """A header carrying a trailing `— qualifier` still matches a bare reference."""
        path = _write(tmp_path, "SKILL.md", _QUALIFIER_SKILL)
        assert check_file(path) == []

    def test_multiple_modes_all_matched(self, tmp_path: Path) -> None:
        """Several dispatch forms each resolve to their own header — clean."""
        path = _write(tmp_path, "SKILL.md", _MULTI_MODE_SKILL)
        assert check_file(path) == []

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """An unreadable path yields no findings rather than raising."""
        assert check_file(tmp_path / "does-not-exist.md") == []


# ---------------------------------------------------------------------------
# find_skill_files
# ---------------------------------------------------------------------------


class TestFindSkillFiles:
    def test_globs_skill_md_under_dir(self, tmp_path: Path) -> None:
        """Discover nested skill definitions beneath the scan directory."""
        target = tmp_path / "foundry" / "skills" / "audit" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("# audit", encoding="utf-8")
        # Non-matching depth should be ignored.
        (tmp_path / "foundry" / "SKILL.md").write_text("x", encoding="utf-8")

        found = find_skill_files(tmp_path)
        assert found == [target]


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


class TestFinding:
    def test_message_format(self) -> None:
        """The message matches the documented MODE-DISPATCH: line."""
        msg = Finding("plugins/cc_foundry/skills/distill/SKILL.md", "Lessons Distillation").message
        assert msg == (
            "MODE-DISPATCH: plugins/cc_foundry/skills/distill/SKILL.md: "
            'references "Mode: Lessons Distillation" with no matching header'
        )


# ---------------------------------------------------------------------------
# main / CLI
# ---------------------------------------------------------------------------


class TestMain:
    def test_clean_files_exit_0(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Explicit clean files exit 0 with a pass line."""
        path = _write(tmp_path, "SKILL.md", _CLEAN_SKILL)
        exit_code = main([str(path)])
        assert exit_code == 0
        assert "✓" in capsys.readouterr().out

    def test_dangling_exit_1(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Report a dangling mode reference as a failure."""
        path = _write(tmp_path, "SKILL.md", _DANGLING_SKILL)
        exit_code = main([str(path)])
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "MODE-DISPATCH:" in out
        assert "Lessons Distillation" in out

    def test_scan_dir_mode(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Discover and validate nested skills from an explicit scan directory."""
        target = tmp_path / "foundry" / "skills" / "distill" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text(_DANGLING_SKILL, encoding="utf-8")

        exit_code = main(["--scan-dir", str(tmp_path)])
        assert exit_code == 1
        assert "MODE-DISPATCH:" in capsys.readouterr().out

    def test_bad_scan_dir_exit_2(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """A non-directory `--scan-dir` is an argument error (exit 2)."""
        exit_code = main(["--scan-dir", str(tmp_path / "nope")])
        assert exit_code == 2
        assert "not a directory" in capsys.readouterr().err

    def test_no_targets_exit_2(self, capsys: pytest.CaptureFixture) -> None:
        """No files and no scan dir is an argument error (exit 2)."""
        exit_code = main([])
        assert exit_code == 2
        assert "no files to check" in capsys.readouterr().err

    @pytest.mark.skipif(not _HAS_PROJECT_PLUGIN_TREE, reason="requires project-root plugins/ tree")
    def test_real_plugins_tree_no_crash(self) -> None:
        """Smoke test against the actual plugins/ tree — must not raise, exit 0 or 1."""
        real_plugins = Path(__file__).resolve().parent.parent.parent.parent  # plugins/
        exit_code = main(["--scan-dir", str(real_plugins)])
        assert exit_code in (0, 1)
