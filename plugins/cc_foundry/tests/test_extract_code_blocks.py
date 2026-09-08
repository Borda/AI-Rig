"""Tests for extract_code_blocks.py — I/O and integration paths.

Pure functions (normalize_lang, estimate_tokens, classify_block, parse_blocks, iter_md_files) are covered by doctests in
the module. This file tests the main() CLI: argument parsing, file walking, filtering flags, and JSONL output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


import extract_code_blocks as ecb


@pytest.fixture(name="md_dir")
def _md_dir(tmp_path: Path) -> Path:
    """Directory with two .md files containing known code blocks."""
    (tmp_path / "a.md").write_text(
        "# Doc\n\n```bash\necho hello\nls -la\n```\n\n```text\nPlain prose sentence.\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "```python\ndef add(x, y):\n    return x + y\n```\n",
        encoding="utf-8",
    )
    return tmp_path


class TestNormalizeLang:
    """normalize_lang: raw fence marker → canonical lowercase language name."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("py", "python", id="py"),
            pytest.param("PYTHON3", "python", id="python3"),
            pytest.param("yml", "yaml", id="yml"),
            pytest.param("sh", "bash", id="sh"),
            pytest.param("tsx", "typescript", id="tsx"),
            pytest.param("rs", "rust", id="rs"),
            pytest.param("", "", id="empty"),
            pytest.param("unknown", "unknown", id="unknown"),
            pytest.param("BASH", "bash", id="bash"),
        ],
    )
    def test_known_aliases(self, raw: str, expected: str) -> None:
        """Known alias maps to canonical name."""
        assert ecb.normalize_lang(raw) == expected


class TestEstimateTokens:
    """estimate_tokens: 4-chars/token heuristic, minimum 1 for non-empty content."""

    def test_empty_returns_zero(self) -> None:
        """Empty string has no tokens."""
        assert ecb.estimate_tokens("") == 0

    def test_single_char_returns_one(self) -> None:
        """Minimum is 1 for any non-empty content."""
        assert ecb.estimate_tokens("x") == 1

    def test_proportional_to_length(self) -> None:
        """Token count scales with character count at 4 chars/token."""
        assert ecb.estimate_tokens("a" * 40) == 10


class TestClassifyBlock:
    """classify_block: three-tier heuristic — known code marker, known non-code marker with content override,
    unknown/empty marker falls back to signal density."""

    @pytest.mark.parametrize("marker", ["bash", "python", "js", "yaml", "sql", "dockerfile"])
    def test_known_code_marker_is_true(self, marker: str) -> None:
        """Known code language marker always returns True."""
        assert ecb.classify_block(marker, "anything") is True

    @pytest.mark.parametrize("marker", ["text", "plain", "markdown", "output", "log"])
    def test_known_non_code_marker_with_prose_is_false(self, marker: str) -> None:
        """Known non-code marker with prose content returns False."""
        assert ecb.classify_block(marker, "This is a prose sentence. Another one here.") is False

    def test_non_code_marker_overridden_by_shell_content(self) -> None:
        """Non-code marker containing shell commands is overridden to True."""
        content = "$ grep pattern file.txt\n$ wc -l out.txt\n$ cat result.json"
        assert ecb.classify_block("output", content) is True

    def test_empty_marker_shebang_is_code(self) -> None:
        """Shebang line triggers code classification with no marker."""
        assert ecb.classify_block("", "#!/usr/bin/env bash\necho hi") is True

    def test_empty_marker_prose_is_not_code(self) -> None:
        """Plain prose with no marker classifies as not code."""
        assert ecb.classify_block("", "This is plain text.\nAnother sentence here.") is False

    def test_empty_marker_shell_vars_is_code(self) -> None:
        """Shell variable expansion triggers code classification."""
        assert ecb.classify_block("", "OUT=$(find . -name '*.py')\necho ${OUT}") is True

    def test_empty_marker_import_is_code(self) -> None:
        """Python import statement triggers code classification."""
        assert ecb.classify_block("", "import os\nimport sys\nresult = os.path.join('a', 'b')") is True

    def test_known_code_marker_always_wins(self) -> None:
        """Known code marker (sh) returns True regardless of content simplicity."""
        assert ecb.classify_block("sh", "ls -la\necho done") is True


class TestParseBlocks:
    """parse_blocks: fenced block extraction — fields, multi-block, edge cases (empty, unclosed, tilde), line
    numbering."""

    def test_basic_block_fields(self) -> None:
        """All fields populated correctly for a single bash block."""
        blocks = ecb.parse_blocks("```bash\necho hi\n```\n", "f.md")
        assert len(blocks) == 1
        b = blocks[0]
        assert b.lang_marker == "bash"
        assert b.lang_detected == "bash"
        assert b.content == "echo hi"
        assert b.line_start == 1
        assert b.line_end == 3
        assert b.is_code is True
        assert b.file == "f.md"

    def test_no_marker_uses_heuristic(self) -> None:
        """Block with no language marker falls back to content heuristic."""
        blocks = ecb.parse_blocks("```\nif [ -f x ]; then echo yes; fi\n```\n", "f.md")
        assert len(blocks) == 1
        assert blocks[0].lang_marker == ""
        assert blocks[0].is_code is True

    def test_multiple_blocks_detected(self) -> None:
        """Multiple consecutive blocks all extracted with correct lang_detected."""
        blocks = ecb.parse_blocks("```py\nx = 1\n```\n\n```sh\nls\n```\n", "f.md")
        assert len(blocks) == 2
        assert blocks[0].lang_detected == "python"
        assert blocks[1].lang_detected == "bash"

    def test_empty_block_skipped(self) -> None:
        """Whitespace-only block body is not emitted."""
        assert ecb.parse_blocks("```bash\n   \n```\n", "f.md") == []

    def test_unclosed_fence_skipped(self) -> None:
        """Fence with no closing line produces no blocks."""
        assert ecb.parse_blocks("```bash\necho hi\n", "f.md") == []

    def test_tilde_fence_supported(self) -> None:
        """Tilde fences (~~~) are parsed the same as backtick fences."""
        blocks = ecb.parse_blocks("~~~python\ndef f(): pass\n~~~\n", "f.md")
        assert len(blocks) == 1
        assert blocks[0].lang_detected == "python"

    def test_line_numbers_correct(self) -> None:
        """line_start and line_end are 1-based and include the fence lines."""
        blocks = ecb.parse_blocks("preamble\n```bash\necho a\necho b\n```\npostamble\n", "f.md")
        assert blocks[0].line_start == 2
        assert blocks[0].line_end == 5

    def test_non_code_marker_classified_false(self) -> None:
        """Text-marked block with prose content has is_code=False."""
        blocks = ecb.parse_blocks("```text\nThis is a plain sentence.\n```\n", "f.md")
        assert len(blocks) == 1
        assert blocks[0].is_code is False


class TestIterMdFiles:
    """iter_md_files: directory walk returning sorted .md file paths, with custom pattern support."""

    def test_finds_md_excludes_other(self, tmp_path: Path) -> None:
        """Only .md files are returned; other extensions excluded."""
        (tmp_path / "a.md").write_text("x")
        (tmp_path / "b.txt").write_text("x")
        assert [os.path.basename(p) for p in ecb.iter_md_files(str(tmp_path))] == ["a.md"]

    def test_recurses_subdirectories(self, tmp_path: Path) -> None:
        """Files in subdirectories are included."""
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.md").write_text("x")
        (tmp_path / "sub" / "b.md").write_text("x")
        assert len(ecb.iter_md_files(str(tmp_path))) == 2

    def test_result_is_sorted(self, tmp_path: Path) -> None:
        """Returned paths are in sorted order."""
        for name in ["c.md", "a.md", "b.md"]:
            (tmp_path / name).write_text("x")
        result = [os.path.basename(p) for p in ecb.iter_md_files(str(tmp_path))]
        assert result == sorted(result)

    def test_custom_pattern(self, tmp_path: Path) -> None:
        """Custom glob pattern selects only matching files."""
        (tmp_path / "a.md").write_text("x")
        (tmp_path / "b.txt").write_text("x")
        result = ecb.iter_md_files(str(tmp_path), pattern="*.txt")
        assert [os.path.basename(p) for p in result] == ["b.txt"]


class TestMain:
    """Main: CLI integration — JSONL output correctness, filtering flags (``--all``, ``--min-tokens``, ``--include``),
    error handling."""

    def test_default_code_only(self, md_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Default mode emits only is_code=True blocks."""
        rc = ecb.main([str(md_dir)])
        assert rc == 0
        objects = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert all(obj["is_code"] for obj in objects)
        langs = {obj["lang_detected"] for obj in objects}
        assert "bash" in langs
        assert "python" in langs

    def test_include_all_emits_non_code(self, md_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify command-line option behavior.

        --all flag includes is_code=False blocks.
        """
        ecb.main([str(md_dir), "--all"])
        objects = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert False in {obj["is_code"] for obj in objects}

    def test_min_tokens_filters_small_blocks(self, md_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify command-line option behavior.

        --min-tokens excludes blocks below threshold.
        """
        ecb.main([str(md_dir), "--min-tokens", "10"])
        objects = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert all(obj["token_estimate"] >= 10 for obj in objects)

    def test_bad_dir_returns_exit_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-existent directory returns exit code 1 with error message."""
        assert ecb.main(["/nonexistent/path/xyz"]) == 1
        assert "not a directory" in capsys.readouterr().err

    def test_jsonl_output_has_all_fields(self, md_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Each output line contains all required fields."""
        ecb.main([str(md_dir)])
        obj = json.loads(capsys.readouterr().out.splitlines()[0])
        assert {
            "file",
            "lang_marker",
            "lang_detected",
            "line_start",
            "line_end",
            "token_estimate",
            "is_code",
            "content",
        }.issubset(obj.keys())

    def test_include_pattern_filters_files(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify command-line option behavior.

        --include glob restricts which files are scanned.
        """
        (tmp_path / "notes.md").write_text("```bash\necho hi\n```\n")
        (tmp_path / "SKILL.md").write_text("```python\nimport os\n```\n")
        ecb.main([str(tmp_path), "--include", "SKILL.md"])
        objects = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert all("SKILL.md" in obj["file"] for obj in objects)
        assert len(objects) == 1
