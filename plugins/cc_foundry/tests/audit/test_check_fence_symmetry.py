"""Tests for check_fence_symmetry bin script.

Covers unclosed fences, bad nesting, valid nesting, and CLI integration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import check_fence_symmetry as cfs


class TestCheckFile:
    """Covers check_file() for individual file scenarios."""

    def test_clean_simple_fence_returns_empty(self, tmp_path: Path) -> None:
        """Balanced ```lang / ``` pair returns no violations."""
        f = tmp_path / "ok.md"
        f.write_text("```python\ncode\n```\n", encoding="utf-8")
        assert cfs.check_file(f) == []

    def test_clean_plain_fence_returns_empty(self, tmp_path: Path) -> None:
        """Balanced plain ``` / ``` pair returns no violations."""
        f = tmp_path / "plain.md"
        f.write_text("```\ncode\n```\n", encoding="utf-8")
        assert cfs.check_file(f) == []

    def test_multiple_separate_fences_clean(self, tmp_path: Path) -> None:
        """Two sequential balanced fences both pass."""
        f = tmp_path / "multi.md"
        f.write_text("```bash\necho hi\n```\n\n```python\npass\n```\n", encoding="utf-8")
        assert cfs.check_file(f) == []

    def test_unclosed_fence_detected(self, tmp_path: Path) -> None:
        """Opening fence with no closing returns one violation."""
        f = tmp_path / "unclosed.md"
        f.write_text("```python\nno close\n", encoding="utf-8")
        violations = cfs.check_file(f)
        assert len(violations) == 1
        assert "unclosed" in violations[0]
        assert "line 1" in violations[0]

    def test_timeout_comment_on_closing_fence_detected(self, tmp_path: Path) -> None:
        """Closing fence with trailing comment is treated as opener — both reported."""
        f = tmp_path / "timeout_bug.md"
        f.write_text("```bash\necho hi\n```  # timeout: 3000\n", encoding="utf-8")
        violations = cfs.check_file(f)
        # Line 1 (```bash) is unclosed; line 3 (``` # timeout:) opens a 2nd fence also unclosed.
        assert len(violations) >= 1
        assert any("unclosed" in v for v in violations)

    @pytest.mark.parametrize(
        ("name", "text", "expected_count", "expected_fragments"),
        [
            pytest.param("clean_close", "```bash\necho hi\n```\n", 0, (), id="clean_close"),
            pytest.param("trailing_spaces", "```bash\necho hi\n```   \n", 0, (), id="trailing_spaces"),
            pytest.param(
                "trailing_comment",
                "```bash\necho hi\n```  # timeout: 3000\n",
                3,
                ("nesting violation", "line 1", "line 3"),
                id="trailing_comment",
            ),
            pytest.param(
                "longer_close_count",
                "```bash\necho hi\n````\n",
                3,
                ("nesting violation", "line 1", "line 3"),
                id="longer_close_count",
            ),
            pytest.param(
                "mismatched_close_count",
                "````bash\necho hi\n```\n",
                2,
                ("line 1", "line 3"),
                id="mismatched_close_count",
            ),
        ],
    )
    def test_closing_fence_variants(
        self,
        tmp_path: Path,
        name: str,
        text: str,
        expected_count: int,
        expected_fragments: tuple[str, ...],
    ) -> None:
        """Closing delimiters only close with matching count and no info string."""
        f = tmp_path / f"{name}.md"
        f.write_text(text, encoding="utf-8")
        violations = cfs.check_file(f)
        joined = "\n".join(violations)
        assert len(violations) == expected_count
        for fragment in expected_fragments:
            assert fragment in joined

    def test_valid_nesting_outer_four_inner_three(self, tmp_path: Path) -> None:
        """Outer ```` wrapping inner ``` is valid nesting — no violations."""
        f = tmp_path / "nested_ok.md"
        f.write_text("````markdown\n```python\ncode\n```\n````\n", encoding="utf-8")
        assert cfs.check_file(f) == []

    def test_bad_nesting_same_count_detected(self, tmp_path: Path) -> None:
        """Inner fence with same backtick count as outer returns nesting violation."""
        f = tmp_path / "nest_bad.md"
        f.write_text("```outer\n```inner\ncode\n```\n```\n", encoding="utf-8")
        violations = cfs.check_file(f)
        assert any("nesting violation" in v for v in violations)

    def test_bad_nesting_inner_more_backticks_detected(self, tmp_path: Path) -> None:
        """Inner fence with more backticks than outer returns nesting violation."""
        f = tmp_path / "nest_more.md"
        f.write_text("```outer\n````inner\ncode\n````\n```\n", encoding="utf-8")
        violations = cfs.check_file(f)
        assert any("nesting violation" in v for v in violations)

    def test_unreadable_file_returns_error(self, tmp_path: Path) -> None:
        """Non-existent path returns cannot-read violation instead of raising."""
        fake = tmp_path / "ghost.md"
        result = cfs.check_file(fake)
        assert len(result) == 1
        assert "cannot read" in result[0]

    def test_no_fences_returns_empty(self, tmp_path: Path) -> None:
        """File with no fence delimiters returns no violations."""
        f = tmp_path / "prose.md"
        f.write_text("# Title\n\nJust prose, no code.\n", encoding="utf-8")
        assert cfs.check_file(f) == []

    def test_violation_includes_line_number(self, tmp_path: Path) -> None:
        """Violation message includes the line number of the offending fence."""
        f = tmp_path / "linenum.md"
        f.write_text("\n\n```bash\nno close\n", encoding="utf-8")
        violations = cfs.check_file(f)
        assert any("line 3" in v for v in violations)

    @pytest.mark.parametrize("count", [3, 4, 5])
    def test_various_backtick_counts_balanced(self, tmp_path: Path, count: int) -> None:
        """Fences with 3, 4, or 5 backticks pass when properly closed."""
        backticks = "`" * count
        f = tmp_path / f"fence{count}.md"
        f.write_text(f"{backticks}lang\ncode\n{backticks}\n", encoding="utf-8")
        assert cfs.check_file(f) == []


class TestMain:
    """Covers main() CLI integration."""

    def test_no_files_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Accept an empty file list and report a passing result."""
        rc = cfs.main([])
        out = capsys.readouterr().out
        assert rc == 0
        assert "no files provided" in out

    def test_clean_file_exits_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Single clean file exits 0 with ✓ pass line."""
        f = tmp_path / "clean.md"
        f.write_text("```bash\necho hi\n```\n", encoding="utf-8")
        rc = cfs.main([str(f)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "✓" in out

    def test_violation_exits_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Report an unclosed fence with the stable ``C14b`` diagnostic identifier."""
        f = tmp_path / "bad.md"
        f.write_text("```python\nno close\n", encoding="utf-8")
        rc = cfs.main([str(f)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "! C14b:" in out

    def test_nonexistent_file_skipped_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-existent file path is silently skipped; exits 0."""
        rc = cfs.main(["/tmp/_nonexistent_fence_test_file.md"])
        assert rc == 0

    def test_timeout_flag_accepted(self, tmp_path: Path) -> None:
        """Verify command-line option behavior.

        --timeout flag accepted without affecting exit code.
        """
        f = tmp_path / "clean.md"
        f.write_text("```bash\nok\n```\n", encoding="utf-8")
        rc = cfs.main([str(f), "--timeout", "5"])
        assert rc == 0
