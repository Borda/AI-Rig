"""Tests for check_fence_symmetry bin script.

Covers unclosed fences, bad nesting, valid nesting, and CLI integration.
"""

from __future__ import annotations

from pathlib import Path

import check_fence_symmetry as cfs
import pytest


def _write_doc(tmp_path: Path, text: str, name: str = "doc.md") -> Path:
    """Write ``text`` to ``tmp_path/name`` and return the path."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestCheckFile:
    """Covers check_file() for individual file scenarios."""

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("```python\ncode\n```\n", id="lang-fence"),
            pytest.param("```\ncode\n```\n", id="plain-fence"),
            pytest.param("```bash\necho hi\n```\n\n```python\npass\n```\n", id="two-sequential-fences"),
            pytest.param("````markdown\n```python\ncode\n```\n````\n", id="outer-four-inner-three"),
            pytest.param("# Title\n\nJust prose, no code.\n", id="no-fences"),
        ],
    )
    def test_balanced_text_returns_empty(self, tmp_path: Path, text: str) -> None:
        """Balanced fences, valid nesting, and text with no fences return no violations.

        Covers a language-tagged and a plain fence pair, two sequential fences, an outer four-backtick fence wrapping an
        inner three-backtick one, and prose without any fence delimiter.
        """
        assert cfs.check_file(_write_doc(tmp_path, text)) == []

    @pytest.mark.parametrize(
        ("text", "line"),
        [
            pytest.param("```python\nno close\n", "line 1", id="first-line"),
            pytest.param("\n\n```bash\nno close\n", "line 3", id="after-blank-lines"),
        ],
    )
    def test_unclosed_fence_detected(self, tmp_path: Path, text: str, line: str) -> None:
        """Opening fence with no closing returns one violation naming the fence's line number.

        The reported line must be the opener's own position, including when blank lines precede it.
        """
        violations = cfs.check_file(_write_doc(tmp_path, text))

        assert len(violations) == 1
        assert "unclosed" in violations[0]
        assert line in violations[0]

    @pytest.mark.parametrize(
        ("text", "expected_count", "expected_fragments"),
        [
            pytest.param("```bash\necho hi\n```\n", 0, (), id="clean-close"),
            pytest.param("```bash\necho hi\n```   \n", 0, (), id="trailing-spaces"),
            # A closing fence with a trailing comment is an opener: line 1 stays unclosed and line 3 opens a second one.
            pytest.param(
                "```bash\necho hi\n```  # timeout: 3000\n",
                3,
                ("nesting violation", "unclosed", "line 1", "line 3"),
                id="trailing-comment",
            ),
            pytest.param(
                "```bash\necho hi\n````\n", 3, ("nesting violation", "line 1", "line 3"), id="longer-close-count"
            ),
            pytest.param("````bash\necho hi\n```\n", 2, ("line 1", "line 3"), id="mismatched-close-count"),
        ],
    )
    def test_closing_fence_variants(
        self, tmp_path: Path, text: str, expected_count: int, expected_fragments: tuple[str, ...]
    ) -> None:
        """Closing delimiters only close with matching count and no info string.

        Trailing spaces still close a fence; a trailing comment or a different backtick count does not, and every
        resulting violation names the lines involved.
        """
        violations = cfs.check_file(_write_doc(tmp_path, text))

        joined = "\n".join(violations)
        assert len(violations) == expected_count
        assert all(fragment in joined for fragment in expected_fragments)

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("```outer\n```inner\ncode\n```\n```\n", id="same-backtick-count"),
            pytest.param("```outer\n````inner\ncode\n````\n```\n", id="inner-more-backticks"),
        ],
    )
    def test_bad_nesting_detected(self, tmp_path: Path, text: str) -> None:
        """An inner fence with the same or more backticks than the outer returns a nesting violation."""
        violations = cfs.check_file(_write_doc(tmp_path, text))

        assert any("nesting violation" in v for v in violations)

    def test_unreadable_file_returns_error(self, tmp_path: Path) -> None:
        """Non-existent path returns a cannot-read violation instead of raising."""
        result = cfs.check_file(tmp_path / "ghost.md")

        assert len(result) == 1
        assert "cannot read" in result[0]

    @pytest.mark.parametrize("count", [3, 4, 5])
    def test_various_backtick_counts_balanced(self, tmp_path: Path, count: int) -> None:
        """Fences with 3, 4, or 5 backticks pass when properly closed."""
        backticks = "`" * count

        assert cfs.check_file(_write_doc(tmp_path, f"{backticks}lang\ncode\n{backticks}\n")) == []


class TestMain:
    """Covers main() CLI integration."""

    def test_no_files_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Accept an empty file list and report a passing result."""
        rc = cfs.main([])

        assert rc == 0
        assert "no files provided" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "extra_args",
        [
            pytest.param([], id="no-options"),
            pytest.param(["--timeout", "5"], id="timeout-option-accepted"),
        ],
    )
    def test_clean_file_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], extra_args: list[str]
    ) -> None:
        """A single clean file exits 0 with the ✓ pass line, with or without the ``--timeout`` option.

        ``--timeout`` is accepted for command-line compatibility and must not change the verdict.
        """
        doc = _write_doc(tmp_path, "```bash\necho hi\n```\n")

        rc = cfs.main([str(doc), *extra_args])

        assert rc == 0
        assert "✓" in capsys.readouterr().out

    def test_violation_exits_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Report an unclosed fence with the stable ``C14b`` diagnostic identifier."""
        doc = _write_doc(tmp_path, "```python\nno close\n")

        rc = cfs.main([str(doc)])

        assert rc == 1
        assert "! C14b:" in capsys.readouterr().out

    def test_nonexistent_file_skipped_exits_zero(self, tmp_path: Path) -> None:
        """A non-existent file path is silently skipped and the run exits 0."""
        assert cfs.main([str(tmp_path / "missing.md")]) == 0
