"""Tests for ``bin/release_shipped_elsewhere.py``.

Every scenario runs against a real scratch repository: patch equivalence is Git's own judgement (``git cherry``), so a
faked ``git`` would only test the fake. The history mirrors the incident this script exists for: ``main`` carries the
last in-branch tag ``1.11.0`` while a ``release/latest`` branch cut from it carries tags ``1.11.1`` and ``1.11.2`` that
are not ancestors of ``main``'s HEAD.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import release_setup as rs
import release_shipped_elsewhere as rse

_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false")


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a fixed identity and return stripped stdout; any failure fails the test."""
    done = subprocess.run(["git", *_IDENTITY, "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    """Write ``content`` into ``name``, commit it with ``message`` and return the new sha."""
    (repo / name).write_text(content, encoding="utf-8", newline="\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Return a repository on ``main`` whose only commit is tagged ``1.11.0``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "base.txt", "base\n", "chore: base")
    _git(root, "tag", "1.11.0")
    return root


def _rows(path: Path) -> dict[str, tuple[str, str]]:
    """Parse the output TSV into ``{sha: (category, tag)}``."""
    parsed = [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()]
    return {sha: (category, tag) for sha, category, tag, _evidence in parsed}


class TestClassify:
    """Range commits are matched against the off-branch tag by patch, then by PR number."""

    def test_backported_commit_is_shipped_and_new_work_is_untouched(self, repo: Path, tmp_path: Path) -> None:
        """A cherry-picked fix is ``shipped``; a commit only on ``main`` never appears in the output.

        This is the reported failure: the fix reached users in 1.11.1, so ``1.11.0..HEAD`` must not call it new.
        """
        fixed = _commit(repo, "a.txt", "a\n", "fix: a (#1)")
        _git(repo, "checkout", "-q", "-b", "release/latest", "1.11.0")
        _git(repo, "cherry-pick", "-x", fixed)
        _git(repo, "tag", "1.11.1")
        _git(repo, "checkout", "-q", "main")
        _commit(repo, "b.txt", "b\n", "feat: b (#2)")
        out = tmp_path / "shipped.tsv"

        code = rse.main(["--repo", str(repo), "--range", "1.11.0..HEAD", "--tags", "1.11.1", "--out", str(out)])

        assert code == 0
        assert _rows(out) == {fixed: ("shipped", "1.11.1")}

    def test_summary_line_counts_candidates_shipped_and_review(
        self, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The stdout summary reports the three counts the release report quotes in its scope line."""
        fixed = _commit(repo, "a.txt", "a\n", "fix: a (#1)")
        _git(repo, "checkout", "-q", "-b", "release/latest", "1.11.0")
        _git(repo, "cherry-pick", "-x", fixed)
        _git(repo, "tag", "1.11.1")
        _git(repo, "checkout", "-q", "main")
        _commit(repo, "b.txt", "b\n", "feat: b (#2)")

        rse.main(["--repo", str(repo), "--range", "1.11.0..HEAD", "--tags", "1.11.1", "--out", str(tmp_path / "o.tsv")])

        assert capsys.readouterr().out.strip() == "candidates=2 shipped=1 review=0"

    def test_same_pr_number_with_different_patch_is_review_not_shipped(self, repo: Path, tmp_path: Path) -> None:
        """A backport that re-implements the change shares only the PR number, so it is kept for verification.

        Subject or PR-number matches are weak evidence: dropping on them could hide real, unreleased work.
        """
        original = _commit(repo, "a.txt", "line one\nline two\n", "fix: a (#7)")
        _git(repo, "checkout", "-q", "-b", "release/latest", "1.11.0")
        _commit(repo, "a.txt", "line one reworked\n", "fix: a on the release line (#7)")
        _git(repo, "tag", "1.11.1")
        _git(repo, "checkout", "-q", "main")
        out = tmp_path / "shipped.tsv"

        assert rse.main(["--repo", str(repo), "--range", "1.11.0..HEAD", "--tags", "1.11.1", "--out", str(out)]) == 0

        assert _rows(out) == {original: ("review", "1.11.1")}

    def test_patch_equivalent_commit_reverted_on_release_side_is_review(self, repo: Path, tmp_path: Path) -> None:
        """A backport that the release line later reverted did not ship as active behaviour, so it is not subtracted."""
        fixed = _commit(repo, "a.txt", "a\n", "fix: a (#1)")
        _git(repo, "checkout", "-q", "-b", "release/latest", "1.11.0")
        _git(repo, "cherry-pick", "-x", fixed)
        _git(repo, "revert", "--no-edit", "HEAD")
        _git(repo, "tag", "1.11.1")
        _git(repo, "checkout", "-q", "main")
        out = tmp_path / "shipped.tsv"

        assert rse.main(["--repo", str(repo), "--range", "1.11.0..HEAD", "--tags", "1.11.1", "--out", str(out)]) == 0

        assert _rows(out) == {fixed: ("review", "1.11.1")}

    def test_shipped_wins_over_review_across_several_tags(self, repo: Path, tmp_path: Path) -> None:
        """A commit weakly matched by one tag and patch-equivalent in another is reported as ``shipped``."""
        fixed = _commit(repo, "a.txt", "a\n", "fix: a (#1)")
        _git(repo, "checkout", "-q", "-b", "release/latest", "1.11.0")
        _commit(repo, "x.txt", "x\n", "fix: other (#1)")
        _git(repo, "tag", "1.11.1")
        _git(repo, "cherry-pick", "-x", fixed)
        _git(repo, "tag", "1.11.2")
        _git(repo, "checkout", "-q", "main")
        out = tmp_path / "shipped.tsv"

        assert (
            rse.main(["--repo", str(repo), "--range", "1.11.0..HEAD", "--tags", "1.11.1 1.11.2", "--out", str(out)])
            == 0
        )

        assert _rows(out)[fixed] == ("shipped", "1.11.2")


class TestFailures:
    """Bad input and git failures are loud: an empty result must never stand in for an error."""

    def test_git_failure_exits_2_and_writes_nothing(self, repo: Path, tmp_path: Path) -> None:
        """An unknown tag stops with exit 2 instead of reporting that nothing shipped elsewhere."""
        out = tmp_path / "shipped.tsv"

        code = rse.main(["--repo", str(repo), "--range", "1.11.0..HEAD", "--tags", "no-such-tag", "--out", str(out)])

        assert code == 2
        assert not out.exists()

    @pytest.mark.parametrize(
        ("range_spec", "tags"),
        [
            pytest.param("1.11.0", "1.11.1", id="range-without-two-dots"),
            pytest.param("1.11.0...HEAD", "1.11.1", id="symmetric-range"),
            pytest.param("--output=x..HEAD", "1.11.1", id="option-like-range"),
            pytest.param("1.11.0..HEAD", "--output=x", id="option-like-tag"),
        ],
    )
    def test_invalid_arguments_exit_1(self, repo: Path, tmp_path: Path, range_spec: str, tags: str) -> None:
        """A malformed range or an option-like value is refused before git runs.

        The ``--flag=value`` spelling lets an option-like value past argparse, so the script's own guard is what runs.
        """
        out = tmp_path / "shipped.tsv"

        argv = ["--repo", str(repo), f"--range={range_spec}", f"--tags={tags}", f"--out={out}"]

        assert rse.main(argv) == 1

    def test_empty_tag_list_writes_an_empty_file(self, repo: Path, tmp_path: Path) -> None:
        """No off-branch tags means an empty classification, which downstream readers treat as 'subtract nothing'."""
        out = tmp_path / "shipped.tsv"

        assert rse.main(["--repo", str(repo), "--range", "1.11.0..HEAD", "--tags", " ", "--out", str(out)]) == 0

        assert out.read_text(encoding="utf-8") == ""


class TestSetupDetectsOffBranchTags:
    """``release_setup`` finds the maintenance-branch tags in a real repository, the input this script consumes."""

    def test_lists_patch_tags_cut_off_head_ancestry_newest_first(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tags 1.11.1 and 1.11.2 on ``release/latest`` are listed; the in-branch 1.11.0 and an rc tag are not.

        This is the reported topology: HEAD's first-parent tag is 1.11.0, so ``git describe`` never sees the patch
        releases that already shipped the backported fixes.
        """
        _git(repo, "checkout", "-q", "-b", "release/latest", "1.11.0")
        _commit(repo, "x.txt", "x\n", "fix: x (#9)")
        _git(repo, "tag", "1.11.1")
        _commit(repo, "y.txt", "y\n", "fix: y (#10)")
        _git(repo, "tag", "1.11.2")
        _git(repo, "tag", "1.12.0rc1")
        _git(repo, "checkout", "-q", "main")
        _commit(repo, "b.txt", "b\n", "feat: b (#2)")
        monkeypatch.chdir(repo)

        tags = rs._shipped_elsewhere_tags("git", "1.11.0")

        assert tags == ["1.11.2", "1.11.1"]
