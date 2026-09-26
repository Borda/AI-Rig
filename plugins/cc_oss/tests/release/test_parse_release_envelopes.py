"""Tests for ``bin/parse_release_envelopes.py``.

No subprocesses are involved — the script only parses two JSON envelopes, checks the files they name, and writes
sentinels under an isolated ``TMPDIR``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import parse_release_envelopes as pre


@pytest.fixture
def tmp_sentinels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point sentinel writes at an isolated temp directory."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


def _sentinel(tmp_path: Path, name: str) -> str:
    """Read one sentinel written by the script, without its trailing newline."""
    return (tmp_path / f"{name}-shared").read_text(encoding="utf-8").rstrip("\n")


def _envelopes(tmp_path: Path, **overrides: object) -> tuple[str, str]:
    """Build a valid pair of envelopes naming real files inside ``tmp_path``."""
    audit = tmp_path / "changelog-audit.md"
    contributors = tmp_path / "contributors.md"
    audit.write_text("audit", encoding="utf-8")
    contributors.write_text("contributors", encoding="utf-8")
    envelope_a = {"status": "done", "file": str(audit), "added": 3, "flagged": 1, "scope_flagged": 0}
    envelope_a.update(overrides)
    envelope_b = {"status": "done", "file": str(contributors), "count": 7}
    return json.dumps(envelope_a), json.dumps(envelope_b)


def test_load_envelope_rejects_non_object() -> None:
    """A JSON array is not an envelope."""
    assert pre.load_envelope("[1, 2]") is None


@pytest.mark.parametrize(
    ("data", "key", "absent", "expected"),
    [
        pytest.param({"status": "done"}, "status", "null", "done", id="present"),
        pytest.param({}, "file", "null", "null", id="missing-renders-null"),
        pytest.param({}, "added", "0", "0", id="missing-with-alternative"),
        pytest.param({"count": 7}, "count", "0", "7", id="number-stringified"),
        pytest.param(None, "status", "null", "", id="unparsable-envelope-empty"),
    ],
)
def test_field_matches_jq_rendering(data: dict | None, key: str, absent: str, expected: str) -> None:
    """Field reads reproduce how ``jq -r`` renders present, missing and null values."""
    assert pre.field(data, key, absent) == expected


def test_main_accepts_valid_envelopes(
    tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Both envelopes valid: paths are re-persisted and the delegation summary is printed."""
    monkeypatch.chdir(tmp_sentinels)
    envelope_a, envelope_b = _envelopes(tmp_sentinels)
    assert pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b]) == 0
    assert _sentinel(tmp_sentinels, "release-changelog-audit").endswith("changelog-audit.md")
    assert _sentinel(tmp_sentinels, "release-contributors").endswith("contributors.md")
    out = capsys.readouterr().out
    assert "3 changelog entries added, 1 flagged, 0 scope-flagged" in out
    assert "7 contributors extracted" in out


def test_main_persists_optional_changelog_file(tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``changelog_file`` is persisted only when the agent returned one."""
    monkeypatch.chdir(tmp_sentinels)
    envelope_a, envelope_b = _envelopes(tmp_sentinels, changelog_file="CHANGELOG.md")
    assert pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b]) == 0
    assert _sentinel(tmp_sentinels, "release-changelog-file") == "CHANGELOG.md"


def test_main_skips_absent_changelog_file(tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``changelog_file`` the sentinel is not created at all."""
    monkeypatch.chdir(tmp_sentinels)
    envelope_a, envelope_b = _envelopes(tmp_sentinels)
    pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b])
    assert not (tmp_sentinels / "release-changelog-file-shared").exists()


def test_main_fails_on_non_done_status(tmp_sentinels: Path, capsys: pytest.CaptureFixture) -> None:
    """A non-``done`` status fails the run before any sentinel is written."""
    envelope_a, envelope_b = _envelopes(tmp_sentinels, status="partial")
    with pytest.raises(SystemExit) as excinfo:
        pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b])
    assert excinfo.value.code == 1
    assert "changelog-audit delegation failed" in capsys.readouterr().err
    assert not (tmp_sentinels / "release-changelog-audit-shared").exists()


def test_main_fails_on_missing_file(
    tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """An envelope naming a file that was never written fails the run."""
    monkeypatch.chdir(tmp_sentinels)
    envelope_a, envelope_b = _envelopes(tmp_sentinels, file=str(tmp_sentinels / "never-written.md"))
    with pytest.raises(SystemExit) as excinfo:
        pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b])
    assert excinfo.value.code == 1
    assert "never-written.md" in capsys.readouterr().err


def test_main_fails_on_unparsable_envelope(tmp_sentinels: Path, capsys: pytest.CaptureFixture) -> None:
    """Garbage in place of an envelope fails rather than silently proceeding."""
    with pytest.raises(SystemExit) as excinfo:
        pre.main(["--envelope-a", "not json", "--envelope-b", "not json"])
    assert excinfo.value.code == 1
    assert "status=, file=" in capsys.readouterr().err


class TestRootContainment:
    """Covers ASEC6: a delegate-named ``file``/``changelog_file`` must resolve inside the working root.

    ``validate()`` previously accepted any path ``Path(path).is_file()`` approved, with no allowed-root check;
    ``changelog_file`` had no validation at all and flowed into ``setup_release_dir.py``'s ``symlink_to`` call, so a
    delegate naming a file outside the working root (e.g. a ``~/.claude`` config) would be linked into a public release
    draft directory.
    """

    def test_rejects_file_outside_root(
        self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A ``file`` that exists but resolves outside the working root fails the run.

        The delegate names a real file one level above the chdir'd working directory — existence alone must not be
        enough, containment is what ASEC6 adds.
        """
        outside = tmp_sentinels / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        workdir = tmp_sentinels / "workdir"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        envelope_a = json.dumps({"status": "done", "file": str(outside)})
        envelope_b = json.dumps({"status": "done", "file": str(outside)})
        with pytest.raises(SystemExit) as excinfo:
            pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b])
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "changelog-audit delegation failed" in err
        assert str(outside) in err

    def test_persists_envelope_spelling_not_resolved_path(
        self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sentinel keeps the delegate's own path spelling, not the resolved form.

        A subagent may return a relative or otherwise non-canonical spelling for a file it wrote; the module docstring
        promises the sentinel carries that exact spelling, so the containment check's own path resolution must not leak
        into the persisted value.
        """
        monkeypatch.chdir(tmp_sentinels)
        spelling = "./changelog-audit.md"
        envelope_a, envelope_b = _envelopes(tmp_sentinels, file=spelling)
        assert pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b]) == 0
        assert _sentinel(tmp_sentinels, "release-changelog-audit") == spelling

    def test_drops_changelog_file_outside_root(
        self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """An out-of-root ``changelog_file`` degrades to a dropped sentinel plus a stdout warning, not a hard failure.

        ``changelog_file`` is optional and ``modes/prepare.md`` re-derives it by search, so this is the load-bearing
        exploit path: it must not propagate a delegate-named out-of-root path into the release symlink target.
        """
        outside_changelog = tmp_sentinels / "CHANGELOG.md"
        outside_changelog.write_text("outside", encoding="utf-8")
        workdir = tmp_sentinels / "workdir"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        envelope_a, envelope_b = _envelopes(workdir, changelog_file=str(outside_changelog))
        assert pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b]) == 0
        assert not (tmp_sentinels / "release-changelog-file-shared").exists()
        captured = capsys.readouterr()
        assert "changelog_file rejected" in captured.out
        assert captured.err == ""

    def test_drops_changelog_file_with_wrong_basename(
        self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """An in-root ``changelog_file`` that isn't named ``CHANGELOG*`` is dropped the same way.

        Containment alone is not sufficient — a delegate could still name an unrelated in-root file for the release
        symlink target, so the basename must also look like the intended changelog.
        """
        monkeypatch.chdir(tmp_sentinels)
        notes = tmp_sentinels / "notes.md"
        notes.write_text("notes", encoding="utf-8")
        envelope_a, envelope_b = _envelopes(tmp_sentinels, changelog_file=str(notes))
        assert pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b]) == 0
        assert not (tmp_sentinels / "release-changelog-file-shared").exists()
        assert "changelog_file rejected" in capsys.readouterr().out

    def test_clears_stale_changelog_file_sentinel(self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stale ``release-changelog-file`` sentinel from an earlier run in the same session is removed.

        The sentinel is keyed by ``CSID``, not by run, so a prior invocation's value would otherwise survive a later run
        that returns no (or a rejected) ``changelog_file``, leaving read-back state pointing at a file the current run
        never named.
        """
        monkeypatch.chdir(tmp_sentinels)
        stale = tmp_sentinels / "release-changelog-file-shared"
        stale.write_text("stale-value\n", encoding="utf-8")
        envelope_a, envelope_b = _envelopes(tmp_sentinels)
        pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b])
        assert not stale.exists()


def test_sentinel_has_trailing_newline(tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sentinels end in a newline — ``IFS= read -r`` fails on a file without one."""
    monkeypatch.chdir(tmp_sentinels)
    envelope_a, envelope_b = _envelopes(tmp_sentinels)
    pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b])
    assert (tmp_sentinels / "release-contributors-shared").read_text(encoding="utf-8").endswith("\n")


class TestDryRun:
    """Covers --dry-run: compute and print, write no session state.

    These sentinels are live skill state keyed by ``CSID``, read by later steps and by PreToolUse hooks. An inspection
    run of this script must not forge them — one such run wrote an ``analyse-report-file`` naming a report that was
    never produced, and the hook denial that followed blocked an unrelated question.
    """

    def test_writes_no_sentinels(
        self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """--dry-run leaves no sentinel behind and exits 0."""
        monkeypatch.chdir(tmp_sentinels)
        envelope_a, envelope_b = _envelopes(tmp_sentinels)
        before = set(tmp_sentinels.glob("release-*"))
        assert pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b, "--dry-run"]) == 0
        assert set(tmp_sentinels.glob("release-*")) == before

    def test_announces_what_it_would_write(
        self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Each suppressed write is still reported, so a verifier sees the computed value."""
        monkeypatch.chdir(tmp_sentinels)
        envelope_a, envelope_b = _envelopes(tmp_sentinels)
        assert pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b, "--dry-run"]) == 0
        assert "[dry-run] would write " in capsys.readouterr().out
