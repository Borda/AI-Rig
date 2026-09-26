"""Tests for check_gitignored_refs bin script.

Covers token extraction, existence/ignore classification against a real temporary git checkout, the waiver marker, and
CLI exit codes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import check_gitignored_refs as cgr


@pytest.fixture(name="repo")
def _repo(tmp_path: Path) -> Path:
    """Minimal git checkout with one gitignored private plan document.

    Mirrors the incident this check guards: `.plans/` is gitignored and holds a
    concrete design document that tracked files must not depend on.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".plans/\ndocs/specs/\n", encoding="utf-8")
    plan = tmp_path / ".plans" / "active" / "plan_secret-design.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("private evidence\n", encoding="utf-8")
    specification = tmp_path / "docs" / "specs" / "private-contract.md"
    specification.parent.mkdir(parents=True)
    specification.write_text("private evidence\n", encoding="utf-8")
    return tmp_path


class TestCandidateTokens:
    """Covers candidate_tokens() line-level extraction."""

    def test_extracts_concrete_plan_path(self) -> None:
        """A literal .plans document path is extracted as one token."""
        line = "Authoritative source: .plans/active/plan_secret-design.md §7.5"
        assert cgr.candidate_tokens(line) == [".plans/active/plan_secret-design.md"]

    def test_extracts_nested_watched_dir(self) -> None:
        """A watched dir nested below another directory is still matched."""
        line = "// see plugins/.plans/active/todo_cost-model.md item 4"
        assert cgr.candidate_tokens(line) == ["plugins/.plans/active/todo_cost-model.md"]

    @pytest.mark.parametrize("line", ["x.plans/active/plan.md", "mydocs/specs/secret.md"])
    def test_watched_name_inside_unrelated_directory_does_not_match(self, line: str) -> None:
        """A watched name must start at a complete path-component boundary."""
        assert cgr.candidate_tokens(line) == []

    def test_extracts_windows_style_path(self) -> None:
        """Backslash-separated watched paths are recognized for native Windows checkouts."""
        assert cgr.candidate_tokens(r"Authority: .plans\active\plan_secret-design.md") == [
            r".plans\active\plan_secret-design.md"
        ]

    def test_waiver_marker_suppresses_line(self) -> None:
        """A line carrying the waiver marker yields no tokens."""
        line = ".plans/active/plan_secret-design.md  <!-- gitignored-ref-ok -->"
        assert cgr.candidate_tokens(line) == []

    def test_placeholder_path_truncates_at_placeholder(self) -> None:
        """A templated path never yields the full document name."""
        tokens = cgr.candidate_tokens("Plan in .plans/active/todo_<name>.md; check in")
        assert tokens == [".plans/active/todo_"]


class TestScanFile:
    """Covers scan_file() classification against a real git checkout."""

    def test_reference_to_existing_ignored_document_is_flagged(self, repo: Path) -> None:
        """A tracked file citing an existing gitignored document is a violation.

        This is the shipped-contract-cites-private-plan incident: the target
        exists only in this checkout and git ignores it.
        """
        source = repo / "CONTRACT.md"
        source.write_text("Authority: .plans/active/plan_secret-design.md\n", encoding="utf-8")

        violations = cgr.scan_file(source, repo)

        assert len(violations) == 1
        assert "plan_secret-design.md" in violations[0]

    def test_second_watched_directory_is_flagged(self, repo: Path) -> None:
        """A concrete ignored docs/specs document is covered by the second watch rule."""
        source = repo / "CONTRACT.md"
        source.write_text("Authority: docs/specs/private-contract.md\n", encoding="utf-8")

        assert len(cgr.scan_file(source, repo)) == 1

    def test_windows_style_reference_is_flagged(self, repo: Path) -> None:
        """A concrete backslash path resolves to the ignored file on every host."""
        source = repo / "CONTRACT.md"
        source.write_text(r"Authority: .plans\active\plan_secret-design.md" + "\n", encoding="utf-8")

        assert len(cgr.scan_file(source, repo)) == 1

    def test_git_check_ignore_error_fails_closed(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A git execution error must not be treated as a clean non-ignored path."""
        monkeypatch.setattr(
            cgr.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 128, stderr=b"fatal"),
        )

        with pytest.raises(RuntimeError, match="git check-ignore failed"):
            cgr.is_ignored(repo / ".plans" / "active" / "plan_secret-design.md", repo)

    def test_illustrative_nonexistent_path_passes(self, repo: Path) -> None:
        """A documentation example naming a file that does not exist here passes.

        Plugins teach *target projects* to use these folders; example paths in READMEs resolve to nothing in this
        checkout and must not be flagged.
        """
        source = repo / "README.md"
        source.write_text("--plan .plans/active/plan_add-streaming-support.md\n", encoding="utf-8")

        assert cgr.scan_file(source, repo) == []

    def test_template_placeholder_path_passes(self, repo: Path) -> None:
        """A workflow template with a <placeholder> segment passes."""
        source = repo / "RULES.md"
        source.write_text("Plan in .plans/active/todo_<name>.md; check in\n", encoding="utf-8")

        assert cgr.scan_file(source, repo) == []

    def test_waiver_marker_passes(self, repo: Path) -> None:
        """A reviewed exception marked gitignored-ref-ok on the same line passes."""
        source = repo / "NOTES.md"
        source.write_text("kept: .plans/active/plan_secret-design.md gitignored-ref-ok\n", encoding="utf-8")

        assert cgr.scan_file(source, repo) == []

    def test_bare_directory_reference_passes(self, repo: Path) -> None:
        """Mentioning the directory convention without a document passes."""
        source = repo / "LAYOUT.md"
        source.write_text("Runtime artifacts live under .plans/active and .plans/closed.\n", encoding="utf-8")

        assert cgr.scan_file(source, repo) == []


class TestMain:
    """Covers main() CLI behavior and exit codes."""

    def test_violation_exits_nonzero(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A violating file produces exit status 1 and names the reference."""
        monkeypatch.chdir(repo)
        source = repo / "CONTRACT.md"
        source.write_text("Authority: .plans/active/plan_secret-design.md\n", encoding="utf-8")

        status = cgr.main(["CONTRACT.md"])

        assert status == 1
        assert "plan_secret-design.md" in capsys.readouterr().out

    def test_clean_files_exit_zero(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clean files and missing paths produce exit status 0."""
        monkeypatch.chdir(repo)
        source = repo / "README.md"
        source.write_text("Use .plans/active/todo_<name>.md in your project.\n", encoding="utf-8")

        assert cgr.main(["README.md", "missing.md"]) == 0

    def test_unrelated_same_basename_is_not_skipped(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only the real checker source, not every same-named file, is exempt."""
        monkeypatch.chdir(repo)
        clone = repo / "check_gitignored_refs.py"
        clone.write_text("ref = '.plans/active/plan_secret-design.md'\n", encoding="utf-8")

        assert cgr.main(["check_gitignored_refs.py"]) == 1

    def test_actual_self_source_is_skipped(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The installed checker source is exempt without suppressing a namesake."""
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cgr, "scan_file", lambda *_: pytest.fail("self source was scanned"))

        assert cgr.main([str(Path(cgr.__file__).resolve())]) == 0
