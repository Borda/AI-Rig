"""Tests for ``bin/resolve_changelog_file.py`` — the single changelog lookup shared by release notes, prepare, audit."""

from __future__ import annotations

from pathlib import Path

import pytest
import resolve_changelog_file as rcf


def _touch_all(root: Path, *relatives: str) -> None:
    """Create a changelog stub at each ``relatives`` path below ``root``, including parent directories."""
    for relative in relatives:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Changelog\n", encoding="utf-8")


class TestResolveChangelog:
    """The fixed changelog search order."""

    @pytest.mark.parametrize(
        ("existing", "expected"),
        [
            pytest.param([], "CHANGELOG.md", id="none-defaults-to-root"),
            pytest.param(["CHANGELOG.md", "docs/CHANGELOG.md"], "CHANGELOG.md", id="root-wins"),
            pytest.param(["docs/CHANGELOG.md", "pkg/CHANGELOG.md"], "docs/CHANGELOG.md", id="docs-before-nested"),
            pytest.param(
                ["zlib/CHANGELOG.rst", "alib/CHANGELOG.md"], "alib/CHANGELOG.md", id="first-nested-alphabetical"
            ),
            pytest.param(
                ["node_modules/CHANGELOG.md", ".venv/CHANGELOG.md"], "CHANGELOG.md", id="skip-dependency-trees"
            ),
            pytest.param(["a/b/CHANGELOG.md"], "CHANGELOG.md", id="only-one-level-deep"),
        ],
    )
    def test_search_order(self, tmp_path: Path, existing: list[str], expected: str) -> None:
        """The fixed search order picks the same file regardless of which release mode asks.

        The repository root wins, then ``docs/``, then the first nested directory alphabetically; dependency trees and
        deeper nesting are ignored, and a repository with no changelog defaults to the root name.
        """
        _touch_all(tmp_path, *existing)

        assert rcf.resolve_changelog(tmp_path) == expected


class TestMain:
    """The command-line entry point: printing, the session sentinel and the failure exit."""

    def test_prints_path_and_writes_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--write-sentinel`` persists the printed path under the session-scoped sentinel name.

        Later release steps run in fresh shells and recover the resolved path from this sentinel.
        """
        _touch_all(tmp_path, "docs/CHANGELOG.md")
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess")

        rc = rcf.main(["--repo-root", str(tmp_path), "--write-sentinel"])

        assert rc == 0
        assert capsys.readouterr().out.strip() == "docs/CHANGELOG.md"
        assert (tmp_path / "release-changelog-file-sess").read_text(encoding="utf-8") == "docs/CHANGELOG.md\n"

    def test_writes_no_sentinel_without_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ``--write-sentinel`` the lookup is read-only.

        Audit and prepare call the helper just to read the path, and must not leave state behind.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "quiet")

        rc = rcf.main(["--repo-root", str(tmp_path)])

        assert rc == 0
        assert not (tmp_path / "release-changelog-file-quiet").exists()

    def test_missing_repo_root_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A non-directory root fails closed with a message instead of defaulting.

        Defaulting to ``CHANGELOG.md`` for a mistyped root would send a later write to the wrong tree.
        """
        rc = rcf.main(["--repo-root", str(tmp_path / "absent")])

        assert rc == 1
        assert "not a directory" in capsys.readouterr().err
