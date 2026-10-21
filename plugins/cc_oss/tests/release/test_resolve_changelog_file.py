"""Tests for ``bin/resolve_changelog_file.py`` — the single changelog lookup shared by release notes, prepare, audit."""

from __future__ import annotations

from pathlib import Path

import pytest
import resolve_changelog_file as rcf


def _touch(root: Path, relative: str) -> None:
    """Create an empty file at ``relative`` below ``root``, including parents."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Changelog\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        pytest.param([], "CHANGELOG.md", id="none-defaults-to-root"),
        pytest.param(["CHANGELOG.md", "docs/CHANGELOG.md"], "CHANGELOG.md", id="root-wins"),
        pytest.param(["docs/CHANGELOG.md", "pkg/CHANGELOG.md"], "docs/CHANGELOG.md", id="docs-before-nested"),
        pytest.param(["zlib/CHANGELOG.rst", "alib/CHANGELOG.md"], "alib/CHANGELOG.md", id="first-nested-alphabetical"),
        pytest.param(["node_modules/CHANGELOG.md", ".venv/CHANGELOG.md"], "CHANGELOG.md", id="skip-dependency-trees"),
        pytest.param(["a/b/CHANGELOG.md"], "CHANGELOG.md", id="only-one-level-deep"),
    ],
)
def test_search_order(tmp_path: Path, existing: list[str], expected: str) -> None:
    """The fixed search order picks the same file regardless of which release mode asks."""
    for relative in existing:
        _touch(tmp_path, relative)
    assert rcf.resolve_changelog(tmp_path) == expected


def test_main_prints_path_and_writes_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--write-sentinel`` persists the printed path under the session-scoped sentinel name."""
    _touch(tmp_path, "docs/CHANGELOG.md")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "sess")

    rc = rcf.main(["--repo-root", str(tmp_path), "--write-sentinel"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "docs/CHANGELOG.md"
    assert (tmp_path / "release-changelog-file-sess").read_text(encoding="utf-8") == "docs/CHANGELOG.md\n"


def test_main_without_flag_writes_no_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``--write-sentinel`` the lookup is read-only."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "quiet")

    assert rcf.main(["--repo-root", str(tmp_path)]) == 0
    assert not (tmp_path / "release-changelog-file-quiet").exists()


def test_missing_repo_root_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A non-directory root fails closed with a message instead of defaulting."""
    assert rcf.main(["--repo-root", str(tmp_path / "absent")]) == 1
    assert "not a directory" in capsys.readouterr().err
