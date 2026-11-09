"""Tests for ``evolution_blame``: SZZ blame evidence per fix, exclusion globs, fingerprint and command line.

Every scenario builds its own deterministic history with the ``evo_repo`` builder. Expected origins are fixed by the
scenario itself (the commit that wrote the repaired line), never read back from the code under test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _evolution_common import check_record
from _evolution_repos import RepoBuilder
from evolution_blame import (
    DEFAULT_EXCLUDES,
    collect_evidence,
    evidence_fingerprint,
    glob_match,
    is_excluded,
    main,
)

#: Root-commit content of ``src/app.py``: one line, written before any tracked feature.
_HEADER = "import os\n"

#: The feature: lines 2-9 of ``src/app.py`` (two blank lines, then a six-line function).
_FEATURE = (
    "\n"
    "\n"
    "def parse_config(text):\n"
    "    entries = {}\n"
    "    for raw_line in text.splitlines():\n"
    "        key, _, value = raw_line.partition('=')\n"
    "        entries[key.strip()] = value.strip()\n"
    "    return entries\n"
)

#: The line every modify-fix repairs, and its repaired form.
_BUGGY = "= value.strip()\n"
_FIXED = "= value.strip() or None\n"

_TEST_FILE = "from app import parse_config\n\n\ndef test_parse():\n    assert parse_config('a=1') == {'a': '1'}\n"


def _edit(repo: RepoBuilder, rel: str, old: str, new: str) -> None:
    """Replace one exact substring in a tracked file (the substring must exist)."""
    text = (repo.path / rel).read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {rel}"
    repo.write(rel, text.replace(old, new, 1))


def _seed(repo: RepoBuilder) -> tuple[str, str]:
    """Commit the root (README, one-line module) and the feature (function plus its test); return both SHAs."""
    repo.write("README.md", "# demo\n")
    repo.write("src/app.py", _HEADER)
    root = repo.commit("chore: init", day=1)
    repo.write("src/app.py", _HEADER + _FEATURE)
    repo.write("tests/test_app.py", _TEST_FILE)
    feature = repo.commit("feat: parse config", day=2)
    return root, feature


def _modify_fix(repo: RepoBuilder, rel: str = "src/app.py", *, day: int) -> str:
    """Commit the one-line repair of the feature's value handling in ``rel``; return its SHA."""
    _edit(repo, rel, _BUGGY, _FIXED)
    return repo.commit("fix: empty values become None", day=day)


def _entry(*shas: str, entry_id: str = "1.1.0/abc123def456") -> dict[str, object]:
    """Build a minimal fix entry linked to the given commits."""
    return {"entry_id": entry_id, "kind": "fix", "release": "1.1.0", "commits": list(shas), "link_method": "sha"}


def _evidence(repo: RepoBuilder, *shas: str, budget_s: float | None = None) -> dict[str, object]:
    """Collect evidence for a fix made of ``shas`` with the default exclusions."""
    return collect_evidence(repo.path, _entry(*shas), exclude_globs=DEFAULT_EXCLUDES, timeout=30, budget_s=budget_s)


def _jsonl_field(path: Path, key: str) -> list[object]:
    """Return one field of every record in a JSONL file, in file order."""
    return [json.loads(line)[key] for line in path.read_text(encoding="utf-8").splitlines()]


def _origins(evidence: dict[str, object]) -> tuple[object, object, object]:
    """Return the status, blamed and context-blamed parts of an evidence record."""
    return evidence["status"], evidence["blamed"], evidence["context_blamed"]


def _noise_rename_scenario(repo: RepoBuilder, *, ignore_file: bool) -> tuple[str, str, str]:
    """Feature, then a variable rename (noise), optionally listed in a committed ignore-revs file, then the fix.

    Returns ``(feature, noise, fix)`` SHAs.
    """
    _, feature = _seed(repo)
    repo.write("src/app.py", (_HEADER + _FEATURE).replace("entries", "result"))
    noise = repo.commit("style: rename local variable", day=3)
    repo.write(".git-blame-ignore-revs" if ignore_file else "src/unrelated.py", f"# bulk rename\n{noise}\n")
    repo.commit("chore: ignore bulk rename in blame", day=4)
    fix = _modify_fix(repo, day=5)
    return feature, noise, fix


class TestGlobMatch:
    """``**``-aware glob matching on POSIX repository paths."""

    @pytest.mark.parametrize(
        ("pattern", "path", "expected"),
        [
            pytest.param("*.md", "README.md", True, id="star-root"),
            pytest.param("*.md", "docs/guide.md", False, id="star-never-crosses-slash"),
            pytest.param("**/*.md", "README.md", True, id="double-star-zero-segments"),
            pytest.param("**/*.md", "a/b/c.md", True, id="double-star-many-segments"),
            pytest.param("tests/**", "tests/unit/deep/test_a.py", True, id="trailing-double-star"),
            pytest.param("tests/**", "src/tests_helper.py", False, id="prefix-is-not-directory"),
            pytest.param("**/tests/**", "pkg/tests/test_x.py", True, id="nested-tests-dir"),
            pytest.param("**/tests/**", "pkg/testsuite/x.py", False, id="segment-must-match-whole"),
            pytest.param("src/**/util.py", "src/util.py", True, id="middle-double-star-zero"),
            pytest.param("src/**/util.py", "src/a/b/util.py", True, id="middle-double-star-many"),
            pytest.param("a/*/c", "a/b/x/c", False, id="single-star-one-segment"),
            pytest.param("**", "any/depth/file.py", True, id="double-star-alone"),
            pytest.param("docs/", "docs/api/index.html", True, id="trailing-slash-directory"),
            pytest.param("*.MD", "README.md", False, id="case-sensitive"),
            pytest.param("**/test_*.py", "src/contest_x.py", False, id="segment-anchored-start"),
        ],
    )
    def test_glob_match_table(self, pattern: str, path: str, expected: bool) -> None:
        """Each pattern/path pair matches exactly as the documented ``**`` semantics say.

        ``*`` stays inside one segment, ``**`` spans zero or more whole segments, matching is case-sensitive and a
        trailing slash means everything below the directory.
        """
        assert glob_match(pattern, path) is expected

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            pytest.param("src/pkg/core.py", False, id="source"),
            pytest.param("src/history.py", False, id="source-named-like-changelog"),
            pytest.param("pkg/tests/test_io.py", True, id="nested-test"),
            pytest.param("src/io_test.py", True, id="suffix-test"),
            pytest.param("src/conftest.py", True, id="conftest"),
            pytest.param("CHANGELOG.md", True, id="changelog"),
            pytest.param("debian/changelog", True, id="bare-changelog"),
            pytest.param("newsfragments/123.bugfix", True, id="towncrier-fragment"),
            pytest.param("docs/conf.py", True, id="docs-tree"),
            pytest.param("uv.lock", True, id="lock"),
            pytest.param("web/package-lock.json", True, id="npm-lock"),
            pytest.param("api/v1/service_pb2.py", True, id="protobuf"),
            pytest.param("static/app.min.js", True, id="minified"),
        ],
    )
    def test_default_excludes_table(self, path: str, expected: bool) -> None:
        """Default exclusions drop tests, docs, changelogs, locks and generated files but keep source modules.

        A source module whose name resembles a changelog (``history.py``) must stay blamable.
        """
        assert is_excluded(path, DEFAULT_EXCLUDES) is expected


class TestCollectEvidence:
    """Blame evidence for single fixes across the SZZ fixture scenarios."""

    def test_modify_fix_blames_feature(self, evo_repo: RepoBuilder) -> None:
        """A fix that rewrites one feature line blames exactly that line on the feature commit.

        Root commit, feature commit adding ``parse_config``, then a one-line fix inside the function. The record
        also lists the touched source file and the changed symbol.
        """
        _, feature = _seed(evo_repo)
        fix = _modify_fix(evo_repo, day=3)
        evidence = _evidence(evo_repo, fix)
        assert _origins(evidence) == ("ok", {feature: 1}, {})
        assert (evidence["files"], evidence["symbols"]) == (["src/app.py"], ["src/app.py:parse_config"])
        assert check_record("evidence", evidence) == []

    def test_add_only_fix_blames_context(self, evo_repo: RepoBuilder) -> None:
        """An add-only fix (missing guard) blames three context lines on each side, never ``blamed``.

        The guard goes in after ``entries = {}`` (line 5), so context lines 3-8 are all from the feature commit.
        """
        _, feature = _seed(evo_repo)
        _edit(evo_repo, "src/app.py", "entries = {}\n", "entries = {}\n    if not text:\n        return entries\n")
        fix = evo_repo.commit("fix: guard empty input", day=3)
        assert _origins(_evidence(evo_repo, fix)) == ("ok", {}, {feature: 6})

    def test_add_only_at_end_of_file_clamps_context(self, evo_repo: RepoBuilder) -> None:
        """An insertion after the last line blames only the three lines before it.

        The file has nine lines; context after the insertion point would run past the end and is clamped by git.
        """
        _, feature = _seed(evo_repo)
        _edit(evo_repo, "src/app.py", "    return entries\n", "    return entries\n\n\nDEFAULT = {}\n")
        fix = evo_repo.commit("fix: export default", day=3)
        assert _origins(_evidence(evo_repo, fix)) == ("ok", {}, {feature: 3})

    def test_add_into_empty_file_is_unattributable(self, evo_repo: RepoBuilder) -> None:
        """Lines added to a file that was empty at the base have no context to blame and do not crash blame.

        The root commit creates an empty ``src/__init__.py``; the fix only fills it.
        """
        evo_repo.write("src/__init__.py", "")
        evo_repo.commit("chore: package", day=1)
        evo_repo.write("src/__init__.py", "VERSION = '1.0'\n")
        fix = evo_repo.commit("fix: expose version", day=2)
        evidence = _evidence(evo_repo, fix)
        assert (_origins(evidence), evidence["files"]) == (("unattributable", {}, {}), ["src/__init__.py"])

    def test_whitespace_commit_is_not_origin(self, evo_repo: RepoBuilder) -> None:
        """A whitespace-only re-indent between feature and fix never becomes the origin (``-w``).

        The re-indent changes every body line of the function; the fix then repairs one of those lines.
        """
        _, feature = _seed(evo_repo)
        evo_repo.write("src/app.py", (_HEADER + _FEATURE).replace("    ", "  "))
        evo_repo.commit("style: two-space indent", day=3)
        fix = _modify_fix(evo_repo, day=4)
        assert _origins(_evidence(evo_repo, fix)) == ("ok", {feature: 1}, {})

    def test_moved_code_blames_original_commit(self, evo_repo: RepoBuilder) -> None:
        """Code moved to another file in one refactor commit is still attributed to the feature (``-M -C``).

        The refactor deletes ``parse_config`` from ``src/app.py`` and adds it to ``src/config.py`` in the same
        commit; the fix repairs the moved line in its new home.
        """
        _, feature = _seed(evo_repo)
        evo_repo.write("src/app.py", _HEADER)
        evo_repo.write("src/config.py", _FEATURE.lstrip("\n"))
        evo_repo.commit("refactor: move parser", day=3)
        fix = _modify_fix(evo_repo, "src/config.py", day=4)
        assert _origins(_evidence(evo_repo, fix)) == ("ok", {feature: 1}, {})

    def test_merge_fix_uses_first_parent(self, evo_repo: RepoBuilder) -> None:
        """A merge-workflow fix is diffed and blamed against its first parent, so only the fix's change counts.

        The fix lands on a branch while main gains an unrelated commit; diffing against the second parent would
        blame that unrelated commit instead of the feature.
        """
        _, feature = _seed(evo_repo)
        evo_repo.branch("fix-branch")
        evo_repo.checkout("fix-branch")
        _modify_fix(evo_repo, day=3)
        evo_repo.checkout("main")
        evo_repo.write("src/other.py", "VALUE = 1\n")
        evo_repo.commit("chore: unrelated", day=4)
        merge = evo_repo.merge("fix-branch", "Merge fix-branch", day=5)
        assert _origins(_evidence(evo_repo, merge)) == ("ok", {feature: 1}, {})

    def test_tests_and_changelog_only_is_unattributable(self, evo_repo: RepoBuilder) -> None:
        """A fix touching only tests and the changelog has no source lines and lists no files or symbols.

        Both touched paths fall under the default exclusions, including the test function's symbol.
        """
        _seed(evo_repo)
        evo_repo.write("CHANGELOG.md", "# Changelog\n\n- Fixed parsing\n")
        _edit(evo_repo, "tests/test_app.py", "{'a': '1'}", "{'a': '1', 'b': None}")
        fix = evo_repo.commit("fix: cover empty values", day=3)
        evidence = _evidence(evo_repo, fix)
        assert _origins(evidence) == ("unattributable", {}, {})
        assert (evidence["files"], evidence["symbols"]) == ([], [])

    def test_pure_rename_is_unattributable(self, evo_repo: RepoBuilder) -> None:
        """A fix that only renames a file changes no line, so nothing is blamed; both paths are listed.

        Negative control: a pure rename must not create an association.
        """
        _seed(evo_repo)
        evo_repo.move("src/app.py", "src/settings.py")
        fix = evo_repo.commit("fix: rename module", day=3)
        evidence = _evidence(evo_repo, fix)
        assert (_origins(evidence), evidence["files"]) == (
            ("unattributable", {}, {}),
            ["src/app.py", "src/settings.py"],
        )

    def test_root_commit_contributes_nothing(self, evo_repo: RepoBuilder) -> None:
        """A fix linked to the root commit has no base to blame at and is unattributable.

        The root commit adds files but has no parent, so it contributes neither lines nor files.
        """
        root, _ = _seed(evo_repo)
        evidence = _evidence(evo_repo, root)
        assert (_origins(evidence), evidence["files"]) == (("unattributable", {}, {}), [])

    def test_own_commits_are_never_origin(self, evo_repo: RepoBuilder) -> None:
        """In a two-commit fix, lines the second commit changes from the first are not blamed on the fix itself.

        Commit one inserts a guard (context blamed on the feature); commit two rewrites that guard, whose only
        origin is commit one, a commit of the same fix.
        """
        _, feature = _seed(evo_repo)
        _edit(evo_repo, "src/app.py", "entries = {}\n", "entries = {}\n    if not text:\n        return entries\n")
        first = evo_repo.commit("fix: guard empty input", day=3)
        _edit(evo_repo, "src/app.py", "if not text:", "if not text or not text.strip():")
        second = evo_repo.commit("fix: guard blank input", day=4)
        assert _origins(_evidence(evo_repo, first, second)) == ("ok", {}, {feature: 6})

    @pytest.mark.parametrize(
        ("ignore_file", "expected_origin"),
        [
            pytest.param(True, "feature", id="listed-noise-is-skipped"),
            pytest.param(False, "noise", id="unlisted-noise-is-origin"),
        ],
    )
    def test_ignore_revs_file_honoured(self, evo_repo: RepoBuilder, ignore_file: bool, expected_origin: str) -> None:
        """A rename commit listed in the base tree's ``.git-blame-ignore-revs`` is skipped; unlisted it is the origin.

        The noise commit renames a variable on the repaired line (not whitespace, so ``-w`` alone cannot hide it).
        """
        feature, noise, fix = _noise_rename_scenario(evo_repo, ignore_file=ignore_file)
        expected = {"feature": feature, "noise": noise}[expected_origin]
        assert _origins(_evidence(evo_repo, fix)) == ("ok", {expected: 1}, {})

    def test_repo_blame_config_is_ignored(self, evo_repo: RepoBuilder, tmp_path: Path) -> None:
        """A ``blame.ignoreRevsFile`` set in the repository config does not change the evidence.

        The config points at a file listing the noise commit, but the base tree has no ignore-revs file, so the
        noise commit stays the origin (only the committed file is honoured and fingerprinted).
        """
        _, noise, fix = _noise_rename_scenario(evo_repo, ignore_file=False)
        revs = tmp_path / "local-ignore-revs"
        revs.write_text(f"{noise}\n", encoding="utf-8")
        evo_repo.git("config", "blame.ignoreRevsFile", str(revs))
        assert _origins(_evidence(evo_repo, fix)) == ("ok", {noise: 1}, {})

    def test_zero_budget_times_out(self, evo_repo: RepoBuilder) -> None:
        """A zero budget yields ``timed_out`` with all partial evidence discarded but a valid fingerprint.

        The fingerprint is computed before the budget starts, so the store can still plan the retry.
        """
        _seed(evo_repo)
        fix = _modify_fix(evo_repo, day=3)
        evidence = _evidence(evo_repo, fix, budget_s=0)
        assert _origins(evidence) == ("timed_out", {}, {})
        assert (evidence["files"], evidence["symbols"]) == ([], [])
        assert evidence["fingerprint"] == evidence_fingerprint(evo_repo.path, _entry(fix), DEFAULT_EXCLUDES)

    def test_no_commits_is_unlinked(self, evo_repo: RepoBuilder) -> None:
        """An entry without commits is ``unlinked`` and runs no git command.

        The empty repository has no commits at all, so any git call against a revision would fail.
        """
        assert _origins(_evidence(evo_repo)) == ("unlinked", {}, {})

    @pytest.mark.parametrize(
        "commit",
        [
            pytest.param("--output=/x", id="option-injection"),
            pytest.param("HEAD~1", id="revision-expression"),
        ],
    )
    def test_non_hex_commit_is_refused(self, evo_repo: RepoBuilder, commit: str) -> None:
        """A commit that is not a hex object name is refused before it can reach git.

        Commit lists derive from untrusted changelog text, so option-like or expression values must never pass.
        """
        with pytest.raises(ValueError, match="hex commit"):
            _evidence(evo_repo, commit)


class TestEvidenceFingerprint:
    """The evidence-stage fingerprint."""

    def test_stable_and_matches_record(self, evo_repo: RepoBuilder) -> None:
        """The fingerprint is stable across calls, abbreviation-independent and stored in the record.

        An abbreviated SHA resolves to the same full commit, so the fingerprint must not change.
        """
        _seed(evo_repo)
        fix = _modify_fix(evo_repo, day=3)
        full = evidence_fingerprint(evo_repo.path, _entry(fix), DEFAULT_EXCLUDES)
        assert full == evidence_fingerprint(evo_repo.path, _entry(fix[:10]), DEFAULT_EXCLUDES)
        assert full == _evidence(evo_repo, fix)["fingerprint"]

    @pytest.mark.parametrize(
        ("excludes", "same"),
        [
            pytest.param(tuple(reversed(DEFAULT_EXCLUDES)), True, id="reordered-same"),
            pytest.param((*DEFAULT_EXCLUDES, *DEFAULT_EXCLUDES[:2]), True, id="duplicates-same"),
            pytest.param((*DEFAULT_EXCLUDES, "src/**"), False, id="added-glob-differs"),
            pytest.param(DEFAULT_EXCLUDES[1:], False, id="removed-glob-differs"),
        ],
    )
    def test_excludes_drive_fingerprint(self, evo_repo: RepoBuilder, excludes: tuple[str, ...], same: bool) -> None:
        """Changing the exclusion set changes the fingerprint; reordering or repeating globs does not.

        Exclusions decide which lines are blamed, so the store must re-blame when they change.
        """
        _seed(evo_repo)
        fix = _modify_fix(evo_repo, day=3)
        baseline = evidence_fingerprint(evo_repo.path, _entry(fix), DEFAULT_EXCLUDES)
        assert (evidence_fingerprint(evo_repo.path, _entry(fix), excludes) == baseline) is same

    def test_ignore_revs_content_drives_fingerprint(self, evo_repo: RepoBuilder) -> None:
        """Editing ``.git-blame-ignore-revs`` in the base tree changes the fingerprint of the next fix.

        Two fixes are compared against the same entry ID: one whose base lists the noise commit and one whose
        base has an extra comment line in the file.
        """
        _, _, fix = _noise_rename_scenario(evo_repo, ignore_file=True)
        before = evidence_fingerprint(evo_repo.path, _entry(fix), DEFAULT_EXCLUDES)
        evo_repo.git("reset", "-q", "--hard", f"{fix}~1")
        evo_repo.write(".git-blame-ignore-revs", (evo_repo.path / ".git-blame-ignore-revs").read_text() + "# more\n")
        evo_repo.commit("chore: annotate ignore list", day=6)
        refix = _modify_fix(evo_repo, day=7)
        assert before != evidence_fingerprint(evo_repo.path, _entry(refix), DEFAULT_EXCLUDES)


class TestCommandLine:
    """The ``evidence`` subcommand."""

    def test_writes_one_record_per_entry(self, evo_repo: RepoBuilder, tmp_path: Path) -> None:
        """A ``build_entries`` result with two fixes produces two JSONL records and exit code 0.

        One fix is linked and blamable, the other has no commits; the summary line names the output file.
        """
        _seed(evo_repo)
        fix = _modify_fix(evo_repo, day=3)
        entries = tmp_path / "entries.json"
        entries.write_text(json.dumps({"fixes": [_entry(fix), _entry(entry_id="1.1.0/unlinked")]}), encoding="utf-8")
        out = tmp_path / "out" / "evidence.jsonl"
        args = ["evidence", "--repo", str(evo_repo.path), "--entries", str(entries), "--out", str(out)]
        assert main(args) == 0
        assert _jsonl_field(out, "status") == ["ok", "unlinked"]

    def test_bad_entry_is_reported_and_others_kept(
        self, evo_repo: RepoBuilder, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A malformed entry is named on stderr and gives exit code 1 while valid entries are still written.

        The list holds one entry with an option-like commit and one entry without commits.
        """
        _seed(evo_repo)
        entries = tmp_path / "entries.json"
        entries.write_text(json.dumps([_entry("--evil"), _entry(entry_id="1.1.0/ok")]), encoding="utf-8")
        out = tmp_path / "evidence.jsonl"
        args = ["evidence", "--repo", str(evo_repo.path), "--entries", str(entries), "--out", str(out)]
        assert main(args) == 1
        assert "hex commit" in capsys.readouterr().err
        assert _jsonl_field(out, "fix_id") == ["1.1.0/ok"]

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("{not json", id="invalid-json"),
            pytest.param('{"columns": []}', id="no-fixes-key"),
            pytest.param("[1, 2]", id="not-entries"),
        ],
    )
    def test_unusable_entries_file_exits_2(self, evo_repo: RepoBuilder, tmp_path: Path, payload: str) -> None:
        """An entries file that is not JSON or not a list of entries stops the run with exit code 2.

        No evidence file is written in that case.
        """
        entries = tmp_path / "entries.json"
        entries.write_text(payload, encoding="utf-8")
        out = tmp_path / "evidence.jsonl"
        args = ["evidence", "--repo", str(evo_repo.path), "--entries", str(entries), "--out", str(out)]
        assert (main(args), out.exists()) == (2, False)
