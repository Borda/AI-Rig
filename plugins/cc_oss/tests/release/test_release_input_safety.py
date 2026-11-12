"""Verify release inputs cannot redirect instruction reads, Git options, or sentinel writes."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest
import release_append_publish as publisher
import release_setup as setup
import release_shipped_elsewhere as shipped
import resolve_changelog_file as changelog


def _can_symlink() -> bool:
    """Probe whether this process can create filesystem symlinks."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        try:
            (root / "link").symlink_to(root / "target")
        except OSError:
            return False
    return True


_requires_symlinks = pytest.mark.skipif(not _can_symlink(), reason="Filesystem symlinks are unavailable")


@pytest.mark.parametrize("marker", ["--all", "HEAD", "a" * 40 + "\n"])
@mock.patch("release_append_publish.subprocess.run", autospec=True)
def test_begin_refuses_non_sha_marker(run: mock.Mock, tmp_path: Path, marker: str) -> None:
    """A range endpoint must be a bare hexadecimal SHA before reaching merge-base."""
    run.return_value = subprocess.CompletedProcess([], 0, stdout="a" * 40)

    with pytest.raises(ValueError, match="marker SHA"):
        publisher.begin(tmp_path, "main", "CHANGELOG.md", marker_sha=marker, range_start="HEAD")


@mock.patch("release_append_publish.subprocess.run", autospec=True)
def test_begin_terminates_merge_base_options(run: mock.Mock, tmp_path: Path) -> None:
    """A valid endpoint is passed after Git's explicit option terminator."""
    marker = "b" * 40
    head = "a" * 40
    run.side_effect = [subprocess.CompletedProcess([], 0, stdout=head), subprocess.CompletedProcess([], 1)]

    with pytest.raises(ValueError, match="not on gathered HEAD"):
        publisher.begin(tmp_path, "main", "CHANGELOG.md", marker_sha=marker, range_start="HEAD")

    assert run.call_args.args[0] == ["git", "merge-base", "--is-ancestor", "--end-of-options", marker, head]


def test_setup_refuses_unresolved_skill_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolver's refused fallback must never become an instruction directory."""
    monkeypatch.setattr(setup, "resolve_plugin_dir", lambda *_args: ("plugins/cc_oss/skills/release", -1))

    with pytest.raises(ValueError, match="release skill directory"):
        setup._resolve_skill_dir()


def test_setup_reports_refused_skill_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI reports a refused skill directory and publishes no setup state."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(setup, "resolve_plugin_dir", lambda *_args: ("plugins/cc_oss/skills/release", -1))

    assert setup.main([]) == 1

    assert "release skill directory" in capsys.readouterr().err
    assert not (tmp_path / "release-setup-shared").exists()


def test_revert_subject_with_long_whitespace_is_bounded() -> None:
    """Long internal whitespace retains the subject without quadratic matching time."""
    subject = "x" + " " * 100_000 + "y"
    code = "import runpy,sys; m=runpy.run_path(sys.argv[1]); print(next(iter(m['_reverted_subjects']({'x':sys.stdin.read()}))))"

    result = subprocess.run(
        [sys.executable, "-c", code, shipped.__file__],
        input="Revert " + subject,
        text=True,
        capture_output=True,
        check=True,
        timeout=3,
    )

    assert result.stdout == subject + "\n"


@pytest.mark.parametrize("subject", ['Revert "fix: x"', '  Revert "fix: x"  ', "Revert fix: x  "])
def test_revert_subject_preserves_quoted_and_unquoted_titles(subject: str) -> None:
    """Whitespace normalization does not add quotes to the reverted subject."""
    assert shipped._reverted_subjects({"sha": subject}) == {"fix: x"}


@_requires_symlinks
@pytest.mark.parametrize("relative", ["CHANGELOG.md", "docs/CHANGELOG.md", "pkg/CHANGELOG.rst"])
def test_changelog_refuses_symlinked_files(tmp_path: Path, relative: str) -> None:
    """A changelog symlink must never select a file outside the repository."""
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        changelog.resolve_changelog(root)


@_requires_symlinks
def test_changelog_refuses_symlinked_docs_directory(tmp_path: Path) -> None:
    """The preferred docs path must not redirect the changelog search outside the tree."""
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "CHANGELOG.md").write_text("outside", encoding="utf-8")
    (root / "docs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        changelog.resolve_changelog(root)


@_requires_symlinks
def test_changelog_skips_symlinked_package_directory(tmp_path: Path) -> None:
    """A linked package's changelog cannot become a repository release destination."""
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "CHANGELOG.md").write_text("outside", encoding="utf-8")
    (root / "pkg").symlink_to(outside, target_is_directory=True)

    assert changelog.resolve_changelog(root) == "CHANGELOG.md"


@_requires_symlinks
def test_changelog_ignores_unrelated_linked_assets(tmp_path: Path) -> None:
    """An unrelated directory link does not hide a regular package changelog."""
    outside = tmp_path / "assets"
    outside.mkdir()
    root = tmp_path / "repo"
    root.mkdir()
    (root / "assets").symlink_to(outside, target_is_directory=True)
    package = root / "pkg"
    package.mkdir()
    (package / "CHANGELOG.rst").write_text("release notes", encoding="utf-8")

    assert changelog.resolve_changelog(root) == "pkg/CHANGELOG.rst"


@_requires_symlinks
def test_changelog_sentinel_replaces_symlink_without_writing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preplanted sentinel symlink is replaced while its target remains unchanged."""
    outside = tmp_path / "victim"
    outside.write_text("keep", encoding="utf-8")
    sentinel = tmp_path / "release-changelog-file-shared"
    sentinel.symlink_to(outside)
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    assert changelog.main(["--repo-root", str(tmp_path), "--write-sentinel"]) == 0

    assert outside.read_text(encoding="utf-8") == "keep"
    assert not sentinel.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "CHANGELOG.md\n"
