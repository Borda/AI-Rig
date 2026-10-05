"""Tests for ``bin/git_state_snapshot.py``.

Parsers are pure and tested on literal git output; the end-to-end snapshot runs against a throwaway repository in
``tmp_path`` so the JSON keys resolve's gates read are pinned to what real git reports.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import git_state_snapshot as gss
import pytest

_skip_no_git = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available")


def _git(repo: Path, *args: str) -> None:
    """Run one git command in the fixture repository, failing loudly."""
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Build a repository on branch ``feature`` with one commit ahead of ``main``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Snapshot Test")
    _git(root, "config", "user.email", "snapshot@example.invalid")
    (root / "a.py").write_text("a\n", encoding="utf-8", newline="\n")
    _git(root, "add", "a.py")
    _git(root, "commit", "-qm", "base")
    _git(root, "switch", "-q", "-c", "feature")
    (root / "b.py").write_text("b\n", encoding="utf-8", newline="\n")
    _git(root, "add", "b.py")
    _git(root, "commit", "-qm", "feature work")
    return root


class TestParsePorcelain:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("M  a.py\0", gss.StatusLists(staged=["a.py"]), id="staged-only"),
            pytest.param(" M a.py\0", gss.StatusLists(unstaged=["a.py"]), id="unstaged-only"),
            pytest.param("MM a.py\0", gss.StatusLists(staged=["a.py"], unstaged=["a.py"]), id="both-sides"),
            pytest.param("?? new.py\0", gss.StatusLists(untracked=["new.py"]), id="untracked"),
            pytest.param("UU c.py\0AA d.py\0", gss.StatusLists(unmerged=["c.py", "d.py"]), id="unmerged-codes"),
            pytest.param(
                "R  new.py\0old.py\0 M z.py\0",
                gss.StatusLists(staged=["new.py"], unstaged=["z.py"]),
                id="rename-skips-source",
            ),
            pytest.param("!! ignored.log\0", gss.StatusLists(), id="ignored-dropped"),
            pytest.param("", gss.StatusLists(), id="clean"),
        ],
    )
    def test_groups_entries(self, raw: str, expected: gss.StatusLists) -> None:
        """Each porcelain code lands in the list resolve's gates read for it.

        The rename case guards the field-skip: without it the source path would be parsed as a bogus entry.
        """
        assert gss.parse_porcelain_z(raw) == expected


class TestParseRemotesAndWorktrees:
    def test_remotes_keep_fetch_urls_only(self) -> None:
        """Push-only lines never overwrite the fetch URL a remote is reported with."""
        raw = "origin\thttps://h/o/r.git (fetch)\norigin\thttps://h/o/r-push.git (push)\nfork\tgit@h:f/r.git (fetch)"
        assert gss.parse_remotes(raw) == {"origin": "https://h/o/r.git", "fork": "git@h:f/r.git"}

    def test_worktrees_report_branch_detached_and_bare(self) -> None:
        """Each porcelain record becomes one entry, with the short branch name and the state flags."""
        raw = "worktree /bare\nbare\n\nworktree /w\nHEAD abc\nbranch refs/heads/feat/x\n\nworktree /d\nHEAD def\ndetached\n"
        assert gss.parse_worktrees(raw) == [
            {"path": "/bare", "head": "", "branch": "", "detached": False, "bare": True},
            {"path": "/w", "head": "abc", "branch": "feat/x", "detached": False, "bare": False},
            {"path": "/d", "head": "def", "branch": "", "detached": True, "bare": False},
        ]


@_skip_no_git
class TestBuildSnapshot:
    def test_reports_branch_base_and_status(self, repo: Path) -> None:
        """A real repository yields branch, HEAD, merge-base against the local base, and the status lists.

        No ``origin`` exists, so the base falls back from ``origin/main`` to the local ``main`` branch.
        """
        (repo / "a.py").write_text("changed\n", encoding="utf-8", newline="\n")
        (repo / "untracked.txt").write_text("u\n", encoding="utf-8", newline="\n")
        snapshot = gss.build_snapshot("main", repo, 10)
        assert snapshot is not None
        main_sha = subprocess.check_output(["git", "rev-parse", "main"], cwd=repo, text=True).strip()
        assert (snapshot["branch"], snapshot["detached"], snapshot["upstream"]) == ("feature", False, None)
        assert snapshot["head_subject"] == "feature work"
        assert (snapshot["base_target"], snapshot["merge_base"]) == ("main", main_sha)
        assert (snapshot["base_ahead"], snapshot["base_behind"]) == (1, 0)
        assert (snapshot["unstaged"], snapshot["untracked"], snapshot["staged"]) == (["a.py"], ["untracked.txt"], [])
        assert snapshot["merge_in_progress"] is False
        assert snapshot["worktrees"][0]["branch"] == "feature"

    def test_unresolvable_base_leaves_base_fields_empty(self, repo: Path) -> None:
        """A base branch that exists nowhere is reported as unresolved, never guessed."""
        snapshot = gss.build_snapshot("no-such-branch", repo, 10)
        assert snapshot is not None
        assert (snapshot["base_target"], snapshot["merge_base"], snapshot["base_ahead"]) == (None, None, None)

    def test_outside_a_repository_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A directory outside any work tree yields no snapshot instead of a half-filled one.

        The ceiling stops git's upward search at ``tmp_path``, so the result never depends on whether the temp root
        itself happens to sit inside some repository.
        """
        outside = tmp_path / "plain"
        outside.mkdir()
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        assert gss.build_snapshot("", outside, 10) is None


@_skip_no_git
class TestMain:
    def test_prints_one_json_line(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The CLI prints exactly one JSON object that resolve can read in a single call."""
        monkeypatch.chdir(repo)
        assert gss.main(["--base-ref", "main"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["branch"] == "feature"

    def test_rejects_leading_dash_base_ref(self, capsys: pytest.CaptureFixture) -> None:
        """A base ref that git would parse as an option is refused before any git call."""
        assert gss.main(["--base-ref=-evil"]) == 1
        assert "must not start with" in json.loads(capsys.readouterr().out)["error"]
