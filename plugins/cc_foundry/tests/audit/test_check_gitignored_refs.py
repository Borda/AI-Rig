"""Tests for check_gitignored_refs bin script.

Covers token extraction, existence/ignore classification against a real temporary git checkout, the waiver marker, and
CLI exit codes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import check_gitignored_refs as cgr
import pytest


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

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param(
                "Authoritative source: .plans/active/plan_secret-design.md §7.5",
                [".plans/active/plan_secret-design.md"],
                id="concrete-plan-path",
            ),
            pytest.param(
                "// see plugins/.plans/active/todo_cost-model.md item 4",
                ["plugins/.plans/active/todo_cost-model.md"],
                id="nested-watched-dir",
            ),
            pytest.param(
                r"Authority: .plans\active\plan_secret-design.md",
                [r".plans\active\plan_secret-design.md"],
                id="windows-style-path",
            ),
            pytest.param(
                "Plan in .plans/active/todo_<name>.md; check in", [".plans/active/todo_"], id="placeholder-truncates"
            ),
        ],
    )
    def test_extracts_watched_path(self, line: str, expected: list[str]) -> None:
        """A literal watched path is extracted as one token, however it is spelled.

        Covers a concrete ``.plans`` document, a watched directory nested below another one, a backslash path from a
        native Windows checkout, and a templated path, which truncates at the placeholder instead of yielding a full
        document name.
        """
        assert cgr.candidate_tokens(line) == expected

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("x.plans/active/plan.md", id="plans-inside-unrelated-name"),
            pytest.param("mydocs/specs/secret.md", id="docs-inside-unrelated-name"),
            pytest.param(".plans/active/plan_secret-design.md  <!-- gitignored-ref-ok -->", id="waiver-marker"),
        ],
    )
    def test_line_yields_no_tokens(self, line: str) -> None:
        """A watched name must start at a complete path-component boundary, and a waived line yields nothing.

        A watched name embedded in an unrelated directory name does not match, and a line carrying the waiver marker is
        suppressed even though it names a concrete document.
        """
        assert cgr.candidate_tokens(line) == []


class TestScanFile:
    """Covers scan_file() classification against a real git checkout."""

    @pytest.mark.parametrize(
        ("text", "fragment"),
        [
            # The shipped-contract-cites-private-plan incident: the target exists here only and git ignores it.
            pytest.param(
                "Authority: .plans/active/plan_secret-design.md\n", "plan_secret-design.md", id="plans-document"
            ),
            pytest.param(
                "Authority: docs/specs/private-contract.md\n", "private-contract.md", id="second-watched-directory"
            ),
            pytest.param(
                r"Authority: .plans\active\plan_secret-design.md" + "\n",
                "plan_secret-design.md",
                id="windows-style-path",
            ),
        ],
    )
    def test_reference_to_existing_ignored_document_is_flagged(self, repo: Path, text: str, fragment: str) -> None:
        """A tracked file citing an existing gitignored document is a violation.

        Covers a ``.plans`` document, a ``docs/specs`` document under the second watch rule, and a backslash path that
        resolves to the ignored file on every host.
        """
        source = repo / "CONTRACT.md"
        source.write_text(text, encoding="utf-8")

        violations = cgr.scan_file(source, repo)

        assert len(violations) == 1
        assert fragment in violations[0]

    def test_git_check_ignore_error_fails_closed(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A git execution error must not be treated as a clean non-ignored path."""
        monkeypatch.setattr(
            cgr.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 128, stderr=b"fatal"),
        )

        with pytest.raises(RuntimeError, match="git check-ignore failed"):
            cgr.is_ignored(repo / ".plans" / "active" / "plan_secret-design.md", repo)

    @pytest.mark.parametrize(
        ("name", "text"),
        [
            # Plugins teach target projects to use these folders; example paths in READMEs resolve to nothing here.
            pytest.param(
                "README.md", "--plan .plans/active/plan_add-streaming-support.md\n", id="illustrative-nonexistent-path"
            ),
            pytest.param(
                "RULES.md", "Plan in .plans/active/todo_<name>.md; check in\n", id="template-placeholder-path"
            ),
            pytest.param(
                "NOTES.md", "kept: .plans/active/plan_secret-design.md gitignored-ref-ok\n", id="waiver-marker"
            ),
            pytest.param(
                "LAYOUT.md",
                "Runtime artifacts live under .plans/active and .plans/closed.\n",
                id="bare-directory-reference",
            ),
        ],
    )
    def test_reference_that_is_not_an_ignored_document_passes(self, repo: Path, name: str, text: str) -> None:
        """Examples, templates, reviewed exceptions and bare directory mentions are not violations.

        Covers a documentation example naming a file that does not exist in this checkout, a workflow template with a
        ``<placeholder>`` segment, a reviewed exception marked ``gitignored-ref-ok`` on the same line, and a mention of
        the directory convention without any document.
        """
        source = repo / name
        source.write_text(text, encoding="utf-8")

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
