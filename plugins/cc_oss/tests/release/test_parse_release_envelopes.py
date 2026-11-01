"""Tests for ``bin/parse_release_envelopes.py``.

No subprocesses are involved — the script only parses two JSON envelopes, checks the files they name, and writes
sentinels under an isolated ``TMPDIR``, with the working directory set to the temp root.
"""

from __future__ import annotations

import json
from pathlib import Path

import parse_release_envelopes as pre
import pytest
from _text_io import read_shared_sentinel


@pytest.fixture
def tmp_sentinels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point sentinel writes and the working root at an isolated temp directory."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _envelopes(tmp_path: Path, **overrides: object) -> tuple[str, str]:
    """Build a valid pair of envelopes naming real files inside ``tmp_path``; ``overrides`` alter the first one."""
    audit = tmp_path / "changelog-audit.md"
    contributors = tmp_path / "contributors.md"
    audit.write_text("audit", encoding="utf-8")
    contributors.write_text("contributors", encoding="utf-8")
    envelope_a = {"status": "done", "file": str(audit), "added": 3, "flagged": 1, "scope_flagged": 0, "unsourced": 2}
    envelope_a.update(overrides)
    envelope_b = {"status": "done", "file": str(contributors), "count": 7}
    return json.dumps(envelope_a), json.dumps(envelope_b)


def _main(envelope_a: str, envelope_b: str, *flags: str) -> int:
    """Run the script on two envelope strings."""
    return pre.main(["--envelope-a", envelope_a, "--envelope-b", envelope_b, *flags])


class TestEnvelopeParsing:
    """load_envelope/field: reading delegate output the way ``jq -r`` would."""

    def test_load_envelope_rejects_non_object(self) -> None:
        """A JSON array is not an envelope.

        Only an object can carry ``status``/``file``; any other JSON type must be treated as unparsable.
        """
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
    def test_field_matches_jq_rendering(self, data: dict | None, key: str, absent: str, expected: str) -> None:
        """Field reads reproduce how ``jq -r`` renders present, missing and null values.

        Skill steps used to read these with ``jq``; the strings printed here must stay byte-compatible with that.
        """
        assert pre.field(data, key, absent) == expected


class TestMain:
    """Main: validate both envelopes, persist their paths as sentinels, and print the delegation summary."""

    def test_accepts_valid_envelopes(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Both envelopes valid: paths are re-persisted and the delegation summary is printed.

        This is the normal hand-off after the two delegates finish: later steps read the sentinels and the user
        sees the counts.
        """
        assert _main(*_envelopes(tmp_sentinels)) == 0
        out = capsys.readouterr().out
        assert (
            read_shared_sentinel(tmp_sentinels, "release-changelog-audit").endswith("changelog-audit.md"),
            read_shared_sentinel(tmp_sentinels, "release-contributors").endswith("contributors.md"),
            "3 changelog entries added, 1 flagged, 0 scope-flagged" in out,
            "2 unsourced" in out,
            "7 contributors extracted" in out,
        ) == (True, True, True, True, True)

    def test_persists_optional_changelog_file(self, tmp_sentinels: Path) -> None:
        """``changelog_file`` is persisted when the agent returned one.

        The changelog path is optional output of the audit delegate; when present the release steps read it back.
        """
        assert _main(*_envelopes(tmp_sentinels, changelog_file="CHANGELOG.md")) == 0
        assert read_shared_sentinel(tmp_sentinels, "release-changelog-file") == "CHANGELOG.md"

    def test_skips_absent_changelog_file(self, tmp_sentinels: Path) -> None:
        """Without ``changelog_file`` the sentinel is not created at all.

        An empty sentinel would read back as a real (empty) path; absence is how later steps detect "not returned".
        """
        _main(*_envelopes(tmp_sentinels))
        assert not (tmp_sentinels / "release-changelog-file-shared").exists()

    def test_fails_on_non_done_status(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A non-``done`` status fails the run before any sentinel is written.

        A partial delegate result must not be recorded as if the audit had completed.
        """
        with pytest.raises(SystemExit) as excinfo:
            _main(*_envelopes(tmp_sentinels, status="partial"))
        assert (
            excinfo.value.code,
            "changelog-audit delegation failed" in capsys.readouterr().err,
            (tmp_sentinels / "release-changelog-audit-shared").exists(),
        ) == (1, True, False)

    def test_fails_on_missing_file(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An envelope naming a file that was never written fails the run.

        ``status: done`` with a dangling path means the delegate lied or crashed after reporting; the missing path is
        named in the error so the failure can be traced.
        """
        with pytest.raises(SystemExit) as excinfo:
            _main(*_envelopes(tmp_sentinels, file=str(tmp_sentinels / "never-written.md")))
        assert (excinfo.value.code, "never-written.md" in capsys.readouterr().err) == (1, True)

    def test_fails_on_unparsable_envelope(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Garbage in place of an envelope fails rather than silently proceeding.

        The error shows empty ``status=`` and ``file=`` so the log makes clear nothing was parsed.
        """
        with pytest.raises(SystemExit) as excinfo:
            _main("not json", "not json")
        assert (excinfo.value.code, "status=, file=" in capsys.readouterr().err) == (1, True)

    def test_sentinel_ends_with_newline(self, tmp_sentinels: Path) -> None:
        """Sentinels end in a newline.

        Skill steps read them with ``IFS= read -r``, which fails on a file that has no trailing newline.
        """
        _main(*_envelopes(tmp_sentinels))
        assert (tmp_sentinels / "release-contributors-shared").read_text(encoding="utf-8").endswith("\n")


class TestRootContainment:
    """A delegate-named ``file``/``changelog_file`` must resolve inside the working root (ASEC6).

    ``validate()`` previously accepted any path ``Path(path).is_file()`` approved, with no allowed-root check;
    ``changelog_file`` had no validation at all and flowed into ``setup_release_dir.py``'s ``symlink_to`` call, so a
    delegate naming a file outside the working root (e.g. a ``~/.claude`` config) would be linked into a public release
    draft directory.
    """

    @pytest.fixture
    def workdir(self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Enter a ``workdir`` below the temp root, so ``tmp_sentinels`` itself lies outside the working root."""
        workdir = tmp_sentinels / "workdir"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        return workdir

    def test_rejects_file_outside_root(
        self, tmp_sentinels: Path, workdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ``file`` that exists but resolves outside the working root fails the run.

        The delegate names a real file one level above the working directory — existence alone must not be enough,
        containment is what ASEC6 adds.
        """
        outside = tmp_sentinels / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        envelope = json.dumps({"status": "done", "file": str(outside)})
        with pytest.raises(SystemExit) as excinfo:
            _main(envelope, envelope)
        err = capsys.readouterr().err
        assert (excinfo.value.code, "changelog-audit delegation failed" in err, str(outside) in err) == (1, True, True)

    def test_persists_envelope_spelling_not_resolved_path(self, tmp_sentinels: Path) -> None:
        """The sentinel keeps the delegate's own path spelling, not the resolved form.

        A subagent may return a relative or non-canonical spelling for a file it wrote; the module docstring promises
        the sentinel carries that exact spelling, so the containment check's own resolution must not leak into it.
        """
        spelling = "./changelog-audit.md"
        assert _main(*_envelopes(tmp_sentinels, file=spelling)) == 0
        assert read_shared_sentinel(tmp_sentinels, "release-changelog-audit") == spelling

    def test_drops_changelog_file_outside_root(
        self, tmp_sentinels: Path, workdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An out-of-root ``changelog_file`` degrades to a dropped sentinel plus a stdout warning, not a failure.

        ``changelog_file`` is optional and ``modes/prepare.md`` re-derives it by search, so this is the load-bearing
        exploit path: it must not propagate a delegate-named out-of-root path into the release symlink target.
        """
        outside_changelog = tmp_sentinels / "CHANGELOG.md"
        outside_changelog.write_text("outside", encoding="utf-8")
        rc = _main(*_envelopes(workdir, changelog_file=str(outside_changelog)))
        captured = capsys.readouterr()
        assert (
            rc,
            (tmp_sentinels / "release-changelog-file-shared").exists(),
            "changelog_file rejected" in captured.out,
            captured.err,
        ) == (0, False, True, "")

    def test_drops_changelog_file_with_wrong_basename(
        self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An in-root ``changelog_file`` that is not named ``CHANGELOG*`` is dropped the same way.

        Containment alone is not sufficient — a delegate could still name an unrelated in-root file for the release
        symlink target, so the basename must also look like the intended changelog.
        """
        notes = tmp_sentinels / "notes.md"
        notes.write_text("notes", encoding="utf-8")
        rc = _main(*_envelopes(tmp_sentinels, changelog_file=str(notes)))
        assert (
            rc,
            (tmp_sentinels / "release-changelog-file-shared").exists(),
            "changelog_file rejected" in capsys.readouterr().out,
        ) == (0, False, True)

    def test_clears_stale_changelog_file_sentinel(self, tmp_sentinels: Path) -> None:
        """A stale ``release-changelog-file`` sentinel from an earlier run in the same session is removed.

        The sentinel is keyed by ``CSID``, not by run, so a prior invocation's value would otherwise survive a later run
        that returns no (or a rejected) ``changelog_file``, leaving read-back state pointing at a file the current run
        never named.
        """
        stale = tmp_sentinels / "release-changelog-file-shared"
        stale.write_text("stale-value\n", encoding="utf-8")
        _main(*_envelopes(tmp_sentinels))
        assert not stale.exists()


class TestDryRun:
    """``--dry-run`` computes and prints but writes no session state.

    These sentinels are live skill state keyed by ``CSID``, read by later steps and by PreToolUse hooks. An inspection
    run of this script must not forge them — one such run wrote an ``analyse-report-file`` naming a report that was
    never produced, and the hook denial that followed blocked an unrelated question.
    """

    def test_writes_no_sentinels(self, tmp_sentinels: Path) -> None:
        """``--dry-run`` exits 0 and leaves no sentinel behind.

        Valid envelopes would normally produce sentinels; with the flag the set of ``release-*`` files must not change.
        """
        envelopes = _envelopes(tmp_sentinels)
        before = set(tmp_sentinels.glob("release-*"))
        assert _main(*envelopes, "--dry-run") == 0
        assert set(tmp_sentinels.glob("release-*")) == before

    def test_announces_what_it_would_write(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Each suppressed write is still reported, so a verifier sees the computed value.

        Silent suppression would make a dry run indistinguishable from a no-op.
        """
        assert _main(*_envelopes(tmp_sentinels), "--dry-run") == 0
        assert "[dry-run] would write " in capsys.readouterr().out
