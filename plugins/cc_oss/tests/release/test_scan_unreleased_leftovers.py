"""Tests for ``bin/scan_unreleased_leftovers.py``.

Regression: a project shipped a release whose docs still described a feature as unreleased. Every test builds a
synthetic repository under ``tmp_path`` and runs the real scanner, so nothing is mocked. They cover a clean repository,
each documentation marker, excluded trees, the changelog Unreleased rule before and after the release heading exists,
and the wiring that makes release audit and prepare run the scan.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import scan_unreleased_leftovers as sul
from _skill_source import read_skill_source

_RELEASE = Path(__file__).resolve().parents[2] / "skills/release"


def _write(root: Path, files: dict[str, str]) -> None:
    """Write each ``files`` entry (relative path to text) below ``root``."""
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def _run(root: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, list[str]]:
    """Run the scanner's ``main`` on ``root`` and return its exit code and stdout rows."""
    code = sul.main(["--repo-root", str(root), *extra])
    rows = [line for line in capsys.readouterr().out.splitlines() if line]
    return code, rows


def test_clean_repository_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Released wording, past versions and an empty Unreleased placeholder produce no rows and exit 0."""
    _write(
        tmp_path,
        {
            "README.md": "# Tool\n\nAdded in 1.2.0. Released features only.\n",
            "docs/api.rst": "API\n===\n\n.. versionadded:: 1.2\n.. versionchanged:: 1.1.0\n",
            "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n\n## [1.2.0]\n\n- shipped\n",
        },
    )
    assert _run(tmp_path, capsys, "--version", "1.2.0") == (0, [])


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        pytest.param("This feature is UNRELEASED.\n", "unreleased", id="unreleased-any-case"),
        pytest.param("Available once it is to be released.\n", "to be released", id="to-be-released"),
        pytest.param("Planned for the upcoming release.\n", "upcoming release", id="upcoming-release"),
        pytest.param("Lands in the next release.\n", "next release", id="next-release"),
        pytest.param("Plugin support: coming soon.\n", "coming soon", id="coming-soon"),
        pytest.param("This API is not yet released.\n", "not yet released", id="not-yet-released"),
        pytest.param("Added in version TBD.\n", "TBD/TODO beside a version", id="tbd-version-word"),
        pytest.param("Since 1.3 (TODO).\n", "TBD/TODO beside a version", id="todo-after-number"),
        pytest.param(".. versionadded:: unreleased\n", "versionadded:: unreleased", id="directive-unreleased"),
        pytest.param(".. versionchanged:: next\n", "versionchanged:: next", id="directive-next"),
        pytest.param(".. deprecated:: dev\n", "deprecated:: dev", id="directive-dev"),
        pytest.param(
            ".. versionadded:: 1.3.0\n", "versionadded:: 1.3.0 newer than target", id="directive-newer-version"
        ),
        pytest.param("```{versionadded} 2.0\n```\n", "versionadded:: 2.0 newer than target", id="myst-directive-newer"),
    ],
)
def test_each_marker_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, marker: str) -> None:
    """Every documented marker yields one ``path:line: <marker>`` row and exit 1."""
    _write(tmp_path, {"docs/guide.md": f"# Guide\n\n{text}"})
    assert _run(tmp_path, capsys, "--version", "1.2.0") == (1, [f"docs/guide.md:3: {marker}"])


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("TODO: rewrite this paragraph.\n", id="todo-without-version"),
        pytest.param(".. versionadded:: 1.2.0\n", id="directive-equal-to-target"),
        pytest.param(".. versionadded:: 0.9\n", id="directive-older"),
        pytest.param("We released it last year.\n", id="released-word"),
    ],
)
def test_non_markers_stay_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str) -> None:
    """Text that only resembles a marker is not reported."""
    _write(tmp_path, {"README.rst": text})
    assert _run(tmp_path, capsys, "--version", "1.2.0") == (0, [])


def test_one_row_per_line_and_sorted_rows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A line matching several markers reports the first one; rows sort by path then line."""
    _write(
        tmp_path,
        {
            "b.txt": "fine\ncoming soon\n",
            "a.md": ".. versionadded:: unreleased -- unreleased, next release\n",
        },
    )
    assert _run(tmp_path, capsys) == (1, ["a.md:1: versionadded:: unreleased", "b.txt:2: coming soon"])


@pytest.mark.parametrize(
    "directory",
    [
        pytest.param("node_modules/pkg", id="node-modules"),
        pytest.param(".venv/lib", id="venv"),
        pytest.param("vendor/lib", id="vendor"),
        pytest.param(".git/info", id="git"),
        pytest.param("releases/1.2.0", id="release-artifacts"),
        pytest.param(".reports/review", id="report-artifacts"),
    ],
)
def test_excluded_trees_are_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str], directory: str) -> None:
    """Dependency, tooling and release-artifact trees are never scanned."""
    _write(tmp_path, {f"{directory}/README.md": "unreleased\n"})
    assert _run(tmp_path, capsys, "--version", "1.2.0") == (0, [])


def test_nested_releases_directory_is_scanned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Only the top-level ``releases/`` output directory is excluded; project docs named releases are scanned."""
    _write(tmp_path, {"docs/releases/notes.md": "Coming soon.\n"})
    assert _run(tmp_path, capsys) == (1, ["docs/releases/notes.md:1: coming soon"])


def test_non_doc_files_and_binaries_are_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Source files outside ``docs/`` and binary files under ``docs/`` are not scanned."""
    _write(tmp_path, {"src/module.py": "# unreleased helper\n"})
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/logo.png").write_bytes(b"\x89PNG\x00unreleased")
    assert _run(tmp_path, capsys) == (0, [])


def test_build_output_under_docs_is_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A stale local Sphinx ``_build`` tree never keeps the gate red once the real docs are fixed."""
    _write(tmp_path, {"docs/_build/html/_sources/guide.rst.txt": "unreleased\n", "docs/guide.rst": "Shipped.\n"})
    assert _run(tmp_path, capsys) == (0, [])


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_ignored_docs_are_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Inside a git work tree, ``.gitignore``-d files are skipped while untracked, unignored docs are still scanned.

    Generated output such as a built site is ignored by the project, so it must not block a release.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path, {".gitignore": "site/\n", "site/index.md": "unreleased\n", "docs/new.md": "coming soon\n"})
    assert _run(tmp_path, capsys) == (1, ["docs/new.md:1: coming soon"])


_LEFTOVER = "changelog Unreleased section holds entries after the target release heading"


@pytest.mark.parametrize(
    ("name", "changelog", "expected"),
    [
        pytest.param(
            "CHANGELOG.md",
            "# Changelog\n\n## [Unreleased]\n\n- new thing\n\n## [1.1.0]\n\n- old\n",
            (0, []),
            id="drafting-before-release-heading",
        ),
        pytest.param(
            "CHANGELOG.md",
            "# Changelog\n\n## [Unreleased]\n\n- new thing\n\n## [1.2.0] - 2026-01-01\n\n- shipped\n",
            (1, [f"CHANGELOG.md:3: {_LEFTOVER}"]),
            id="leftover-after-release-heading",
        ),
        pytest.param(
            "CHANGELOG.md",
            "# Changelog\n\n## Unreleased\n\n[Unreleased]: https://example.invalid/compare\n\n## v1.2\n\n- shipped\n",
            (0, []),
            id="empty-unreleased-with-link-reference",
        ),
        pytest.param(
            "CHANGELOG.md",
            "# Changelog\n\n## [Unreleased]\n\n### Added\n\n### Fixed\n\n## [1.2.0]\n\n- shipped\n",
            (0, []),
            id="empty-subheading-skeleton",
        ),
        pytest.param(
            "CHANGELOG.md",
            "# Changelog\n\n## Unreleased\n\n### Added\n\n- new\n\n## Version 1.2.0\n\n- shipped\n",
            (1, [f"CHANGELOG.md:3: {_LEFTOVER}"]),
            id="version-word-heading",
        ),
        pytest.param(
            "CHANGELOG.rst",
            "Changelog\n=========\n\nUnreleased\n----------\n\n- new\n\n1.2.0\n-----\n\n- shipped\n",
            (1, [f"CHANGELOG.rst:4: {_LEFTOVER}"]),
            id="rst-leftover",
        ),
        pytest.param(
            "CHANGELOG.md",
            "# Changelog\n\n## 1.2.0\n\n- the next release will be unreleased-free\n",
            (0, []),
            id="history-text-is-not-a-phrase-hit",
        ),
    ],
)
def test_changelog_unreleased_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, changelog: str, expected: tuple[int, list[str]]
) -> None:
    """An Unreleased section with entries is a hit only once the target version heading exists."""
    _write(tmp_path, {name: changelog})
    assert _run(tmp_path, capsys, "--version", "v1.2.0") == expected


def test_explicit_changelog_path_gets_changelog_rule(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--changelog`` marks a differently named file as release history."""
    _write(tmp_path, {"docs/release-log.md": "## Unreleased\n\n- new\n\n## 1.2.0\n\n- shipped\n"})
    code, rows = _run(tmp_path, capsys, "--version", "1.2.0", "--changelog", "docs/release-log.md")
    assert (code, rows) == (
        1,
        ["docs/release-log.md:1: changelog Unreleased section holds entries after the target release heading"],
    )


def test_without_version_skips_comparison_and_changelog_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No target version: newer-version directives and the changelog rule are skipped, phrases still apply."""
    _write(
        tmp_path,
        {
            "docs/a.rst": ".. versionadded:: 9.0\nnot yet released\n",
            "CHANGELOG.md": "## Unreleased\n\n- x\n\n## 1.0.0\n",
        },
    )
    assert _run(tmp_path, capsys) == (1, ["docs/a.rst:2: not yet released"])


@pytest.mark.parametrize(
    ("subdir", "argv"),
    [
        pytest.param(".", ["--version", "next"], id="unparsable-version"),
        pytest.param("absent", [], id="missing-root"),
    ],
)
def test_invalid_arguments_exit_two(tmp_path: Path, subdir: str, argv: list[str]) -> None:
    """An unparsable version or a missing root is a usage error, not a clean scan."""
    assert sul.main(["--repo-root", str(tmp_path / subdir), *argv]) == 2


@pytest.mark.parametrize(
    ("relative", "phrases"),
    [
        pytest.param("modes/audit.md", ("scan_unreleased_leftovers.py", "`high`"), id="audit"),
        pytest.param("modes/prepare.md", ("scan_unreleased_leftovers.py", "stop"), id="prepare"),
        pytest.param("templates/audit-checks.md", ("Unreleased leftovers",), id="readiness-row"),
    ],
)
def test_release_modes_run_the_scan(relative: str, phrases: tuple[str, ...]) -> None:
    """Release audit and prepare wire the scan, and the readiness table carries its row."""
    lines = read_skill_source(_RELEASE / relative).splitlines()
    assert [p for p in phrases if not any(p in line for line in lines)] == []
