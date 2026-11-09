"""Tests for ``bin/resolve_pr_refs.py``.

Default-branch parsing and PR-metadata extraction are pure; the git and ``gh`` calls go through a patched
``subprocess.run`` so no real repository or network is touched. Sentinel assertions read back the files written under an
isolated ``TMPDIR`` with a pinned session token. One test deliberately runs real ``git check-ref-format`` to keep the
hand-written branch-name check honest.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
import resolve_pr_refs as rpr
from _process import done
from _text_io import read_shared_sentinel


def _meta(**overrides: object) -> dict:
    """Build a ``gh pr view`` payload for a cross-repo PR."""
    payload = {
        "headRefName": "feature-branch",
        "baseRefName": "main",
        "isCrossRepository": True,
        "headRefOid": "abc123",
        "headRepositoryOwner": {"login": "contributor"},
        "headRepository": {"id": "R_kgDO", "name": "renamed-fork"},
    }
    payload.update(overrides)
    return payload


def _wire(
    run: mock.MagicMock, *, default_ref: str, pr_meta: dict | None, head: str = "abc123", remote_show: str = ""
) -> None:
    """Answer the default-branch lookups, the PR fetch and the local SHA reads through the ``subprocess.run`` mock.

    An empty ``default_ref`` or ``remote_show``, or a ``pr_meta`` of ``None``, makes that lookup fail with exit 1.
    """

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return the canned answer for one git or gh command."""
        if cmd[:2] == ["git", "symbolic-ref"]:
            return done(stdout=default_ref) if default_ref else done(returncode=1)
        if cmd[:2] == ["git", "remote"]:
            return done(stdout=remote_show) if remote_show else done(returncode=1)
        if cmd[:2] == ["git", "rev-parse"]:
            return done(stdout="feature-branch" if "--abbrev-ref" in cmd else head)
        if cmd[0] == "gh":
            return done(stdout=json.dumps(pr_meta)) if pr_meta is not None else done(returncode=1)
        return done(returncode=1)

    run.side_effect = fake_run


@pytest.fixture
def run() -> Iterator[mock.MagicMock]:
    """Patch ``subprocess.run`` in the script with a mock; every command fails with exit 1 until it is wired."""
    with mock.patch.object(rpr.subprocess, "run", autospec=True, return_value=done(returncode=1)) as mocked:
        yield mocked


_BRANCH_NAME_CASES = [
    pytest.param("main", True, id="plain"),
    pytest.param("feature/x-1", True, id="slash"),
    pytest.param("release/v1.2.3", True, id="dots-in-component"),
    pytest.param("bugfix_ünï/ブランチ", True, id="non-ascii"),
    pytest.param("@", True, id="bare-at"),
    pytest.param("", False, id="empty"),
    pytest.param("-foo", False, id="leading-dash"),
    pytest.param("--detach", False, id="long-option"),
    pytest.param("a..b", False, id="double-dot"),
    pytest.param("a@{b", False, id="reflog-syntax"),
    pytest.param("a b", False, id="space"),
    pytest.param("a\nb", False, id="newline"),
    pytest.param("a\x7fb", False, id="del-char"),
    pytest.param("a~b", False, id="tilde"),
    pytest.param("a^b", False, id="caret"),
    pytest.param("a:b", False, id="colon"),
    pytest.param("a?b", False, id="question-mark"),
    pytest.param("a*b", False, id="asterisk"),
    pytest.param("a[b", False, id="bracket"),
    pytest.param("a\\b", False, id="backslash"),
    pytest.param("/a", False, id="leading-slash"),
    pytest.param("a/", False, id="trailing-slash"),
    pytest.param("a//b", False, id="empty-component"),
    pytest.param("a.", False, id="trailing-dot"),
    pytest.param("a/.hidden", False, id="dot-component"),
    pytest.param("a.lock/b", False, id="lock-component"),
]


class TestPureHelpers:
    """Helpers that read git output or PR metadata without any process call."""

    def test_parse_head_branch(self) -> None:
        """The default branch is lifted from the ``HEAD branch:`` line."""
        assert rpr.parse_head_branch("* remote origin\n  HEAD branch: develop\n  Remote branches:") == "develop"

    @pytest.mark.parametrize(
        ("meta", "expected"),
        [
            pytest.param({"headRepositoryOwner": {"login": "contrib"}}, "contrib", id="present"),
            pytest.param({"headRepositoryOwner": None}, "", id="null-owner"),
            pytest.param({}, "", id="absent"),
        ],
    )
    def test_head_repo_owner(self, meta: dict, expected: str) -> None:
        """The fork owner's login is read defensively — a same-repo PR has no owner object."""
        assert rpr.head_repo_owner(meta) == expected

    @pytest.mark.parametrize(
        ("meta", "expected"),
        [
            pytest.param({"headRepository": {"id": "R_1", "name": "renamed-fork"}}, "renamed-fork", id="present"),
            pytest.param({"headRepository": None}, "", id="null-repo"),
            pytest.param({}, "", id="absent"),
        ],
    )
    def test_head_repo_name(self, meta: dict, expected: str) -> None:
        """The head repository's name is read defensively — a deleted fork reports no repository object.

        Step 10 hands the name to ``derive_fork_remote.py --fork-repo``; an empty value keeps its default of reusing the
        upstream name.
        """
        assert rpr.head_repo_name(meta) == expected

    @pytest.mark.parametrize(("name", "valid"), _BRANCH_NAME_CASES)
    def test_is_valid_branch_name(self, name: str, valid: bool) -> None:
        """Names git's branch rules reject, and names that would parse as an option, are refused.

        The PR author controls ``headRefName``/``baseRefName``; the value is later passed to ``git switch`` and ``gh pr
        checkout`` as a bare argument, so the check has to run before the value is persisted.
        """
        assert rpr.is_valid_branch_name(name) is valid

    @pytest.mark.parametrize(("name", "valid"), _BRANCH_NAME_CASES)
    def test_is_valid_branch_name_matches_git(self, tmp_path: Path, name: str, valid: bool) -> None:
        """The pure-Python check agrees with real ``git check-ref-format --branch`` on every case.

        A hand-written copy of git's rules can drift from git; this runs the real command against the same inputs. The
        ``--branch`` form is the one that also refuses a leading ``-``.
        """
        proc = subprocess.run(
            ["git", "check-ref-format", "--branch", name], capture_output=True, cwd=tmp_path, check=False
        )
        assert (proc.returncode == 0) is valid


class TestDefaultBranch:
    """default_branch: find the origin default branch locally first, then through ``git remote show``."""

    @pytest.mark.parametrize(
        ("default_ref", "remote_show", "expected"),
        [
            pytest.param("refs/remotes/origin/main", "", "main", id="local-symbolic-ref"),
            pytest.param("", "  HEAD branch: trunk", "trunk", id="remote-show-fallback"),
        ],
    )
    def test_resolves_default_branch(
        self, run: mock.MagicMock, default_ref: str, remote_show: str, expected: str
    ) -> None:
        """The local symbolic ref answers without a network trip; without it ``git remote show origin`` is parsed."""
        _wire(run, default_ref=default_ref, pr_meta=None, remote_show=remote_show)
        assert rpr.default_branch(5) == expected


class TestFetchPrMeta:
    """fetch_pr_meta: one ``gh pr view`` call, degrading to an empty payload on failure."""

    def test_requests_head_repository(self, run: mock.MagicMock) -> None:
        """The single ``gh pr view`` call asks for ``headRepository``, the source of a renamed fork's name.

        Without the field the payload never carries the name, so a fork renamed after forking would silently get a push
        URL built from the upstream name.
        """
        run.return_value = done(stdout="{}")
        rpr.fetch_pr_meta("42", 5)
        cmd = run.call_args_list[0].args[0]
        assert "headRepository" in cmd[cmd.index("--json") + 1].split(",")

    def test_unusable_response_gives_empty_payload(self, run: mock.MagicMock) -> None:
        """An unusable ``gh`` response degrades to an empty payload rather than raising."""
        run.return_value = done(stdout="not json")
        assert rpr.fetch_pr_meta("42", 5) == {}


@pytest.mark.parametrize(
    "needle",
    [
        pytest.param('IFS= read -r FORK_REPO < "${TMPDIR:-/tmp}/resolve-head-repo-name-${CSID}"', id="reads-sentinel"),
        pytest.param('--fork-repo "$FORK_REPO"', id="passes-fork-repo"),
    ],
)
def test_resolve_step10_push_block_consumes_head_repo_name(needle: str) -> None:
    """``oss:resolve`` Step 10 reads the ``resolve-head-repo-name`` sentinel and hands it to ``--fork-repo``.

    The sentinel is useless unless the push block consumes it: a fork remote missing at push time would otherwise be
    added with the upstream repository name, pointing at a repository the contributor may not own.
    """
    skill = Path(rpr.__file__).resolve().parents[1] / "skills" / "resolve" / "modes" / "push.md"
    assert any(needle in line for line in skill.read_text(encoding="utf-8").splitlines())


class TestMain:
    """Main --pr: validate a PR's refs, then persist every ref later steps reload."""

    def test_rejects_leading_dash_pr(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A ``--pr`` value starting with '-' is refused before any subprocess runs.

        ``--pr`` flows raw into ``gh pr view <pr>`` argv; a leading-dash value would be consumed as a flag rather than
        an operand. Uses the ``--pr=<value>`` single-token form: argparse's own tokenizer treats a bare ``--pr
        --evil-flag`` two-token pair as a *missing* value (unknown-option lookahead), which would exit 2 through
        argparse itself rather than exercising this guard.
        """
        assert rpr.main(["--pr=--evil-flag"]) == 1
        assert "must not start with" in capsys.readouterr().out

    def test_persists_all_sentinels(self, tmp_sentinels: Path, run: mock.MagicMock) -> None:
        """A safe PR writes every ref the later steps reload."""
        _wire(run, default_ref="refs/remotes/origin/main", pr_meta=_meta())
        rc = rpr.main(["--pr", "42"])
        names = [
            "resolve-head-ref",
            "resolve-base-ref",
            "resolve-is-cross-repo",
            "resolve-head-repo-owner",
            "resolve-head-repo-name",
            "resolve-saved-branch",
            "resolve-pr-head-oid",
            "resolve-local-sha",
        ]
        assert rc == 0
        assert {name: read_shared_sentinel(tmp_sentinels, name) for name in names} == {
            "resolve-head-ref": "feature-branch",
            "resolve-base-ref": "main",
            "resolve-is-cross-repo": "true",
            "resolve-head-repo-owner": "contributor",
            "resolve-head-repo-name": "renamed-fork",
            "resolve-saved-branch": "feature-branch",
            "resolve-pr-head-oid": "abc123",
            "resolve-local-sha": "abc123",
        }

    def test_blocks_when_head_is_default_branch(
        self, tmp_sentinels: Path, run: mock.MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A PR whose head is the default branch is refused before any sentinel is written."""
        _wire(run, default_ref="refs/remotes/origin/main", pr_meta=_meta(headRefName="main"))
        assert rpr.main(["--pr", "42"]) == 1
        assert "equals default branch" in capsys.readouterr().out
        assert not (tmp_sentinels / "resolve-head-ref-shared").exists()

    @pytest.mark.parametrize(
        ("overrides", "label"),
        [
            pytest.param({"headRefName": "--detach"}, "head", id="head-option-like"),
            pytest.param({"headRefName": "-c"}, "head", id="head-short-option"),
            pytest.param({"baseRefName": "-foo"}, "base", id="base-option-like"),
            pytest.param({"headRefName": "a..b"}, "head", id="head-double-dot"),
        ],
    )
    def test_blocks_invalid_ref_names_before_persisting(
        self,
        tmp_sentinels: Path,
        run: mock.MagicMock,
        capsys: pytest.CaptureFixture[str],
        overrides: dict[str, str],
        label: str,
    ) -> None:
        """A head or base ref that is not a valid branch name is refused and no sentinel is written.

        Persisting it would hand an attacker-chosen string to ``git switch "$PR_HEAD_REF"`` later, where a leading dash
        is parsed as an option (``--detach`` detaches HEAD instead of switching).
        """
        _wire(run, default_ref="refs/remotes/origin/main", pr_meta=_meta(**overrides))
        assert rpr.main(["--pr", "42"]) == 1
        assert f"PR {label} ref" in capsys.readouterr().out
        assert list(tmp_sentinels.glob("resolve-*")) == []

    def test_blocks_without_default_branch(
        self, tmp_sentinels: Path, run: mock.MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An undeterminable default branch fails closed — the safety check cannot be evaluated."""
        _wire(run, default_ref="", pr_meta=_meta())
        assert rpr.main(["--pr", "42"]) == 1
        assert "cannot determine default branch" in capsys.readouterr().out

    def test_failed_pr_fetch_still_yields_defaults(self, tmp_sentinels: Path, run: mock.MagicMock) -> None:
        """A failed PR fetch still yields usable defaults: base ref from the repo, cross-repo false."""
        _wire(run, default_ref="refs/remotes/origin/main", pr_meta=None)
        rc = rpr.main(["--pr", "42"])
        names = ["resolve-base-ref", "resolve-is-cross-repo", "resolve-pr-head-oid"]
        assert rc == 0
        assert {name: read_shared_sentinel(tmp_sentinels, name) for name in names} == {
            "resolve-base-ref": "main",
            "resolve-is-cross-repo": "false",
            "resolve-pr-head-oid": "",
        }

    def test_emits_state_trace_on_stderr(
        self, tmp_sentinels: Path, run: mock.MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The reflog trace keeps Step 4's opaque branch state visible."""
        _wire(run, default_ref="refs/remotes/origin/main", pr_meta=_meta())
        rpr.main(["--pr", "42"])
        assert "→ Step 4 state: SAVED_BRANCH=feature-branch PR_HEAD_REF=feature-branch" in capsys.readouterr().err

    def test_sentinel_ends_with_newline(self, tmp_sentinels: Path, run: mock.MagicMock) -> None:
        """Sentinels end in a newline — ``IFS= read -r`` fails on a file without one."""
        _wire(run, default_ref="refs/remotes/origin/main", pr_meta=_meta())
        rpr.main(["--pr", "42"])
        assert (tmp_sentinels / "resolve-head-ref-shared").read_text(encoding="utf-8") == "feature-branch\n"


class TestLocalBase:
    """--local-base: the no-PR path publishes only the default branch as the QA base."""

    @pytest.mark.parametrize(
        ("default_ref", "remote_show", "expected"),
        [
            pytest.param("refs/remotes/origin/trunk", "", "trunk", id="local-origin-head"),
            pytest.param("", "* remote origin\n  HEAD branch: develop\n", "develop", id="remote-show-fallback"),
        ],
    )
    def test_publishes_default_branch(
        self, tmp_sentinels: Path, run: mock.MagicMock, default_ref: str, remote_show: str, expected: str
    ) -> None:
        """The default branch lands in ``resolve-base-ref`` without any PR fetch or head-ref sentinel.

        Report mode with no PR number and comment dispatch skip Step 4, so this is the only writer of the base that Step
        9's QA range reads. Without a local origin/HEAD the ``git remote show origin`` answer is used, as on the PR
        path.
        """
        _wire(run, default_ref=default_ref, pr_meta=None, remote_show=remote_show)
        rc = rpr.main(["--local-base"])
        assert (rc, read_shared_sentinel(tmp_sentinels, "resolve-base-ref")) == (0, expected)
        assert not (tmp_sentinels / "resolve-head-ref-shared").exists()

    def test_undeterminable_base_clears_stale_sentinel(
        self, tmp_sentinels: Path, run: mock.MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No default branch exits 1 and clears a stale value so QA cannot reuse the previous run's base."""
        (tmp_sentinels / "resolve-base-ref-shared").write_text("stale\n", encoding="utf-8")
        _wire(run, default_ref="", pr_meta=None)
        assert rpr.main(["--local-base"]) == 1
        assert "Cannot determine a valid origin default branch" in capsys.readouterr().out
        assert read_shared_sentinel(tmp_sentinels, "resolve-base-ref") == ""


class TestDryRun:
    """--dry-run: compute and print, write no session state.

    These sentinels are live skill state keyed by ``CSID``, read by later steps and by PreToolUse hooks. An inspection
    run of this script must not forge them — one such run wrote an ``analyse-report-file`` naming a report that was
    never produced, and the hook denial that followed blocked an unrelated question.
    """

    def test_writes_no_sentinels(self, tmp_sentinels: Path, run: mock.MagicMock) -> None:
        """--dry-run leaves the sentinel directory empty and exits 0."""
        _wire(run, default_ref="refs/remotes/origin/main", pr_meta=_meta())
        assert rpr.main(["--pr", "42", "--dry-run"]) == 0
        assert list(tmp_sentinels.glob("resolve-*")) == []

    def test_announces_what_it_would_write(
        self, tmp_sentinels: Path, run: mock.MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Each suppressed write is still reported, so a verifier sees the computed value."""
        _wire(run, default_ref="refs/remotes/origin/main", pr_meta=_meta())
        assert rpr.main(["--pr", "42", "--dry-run"]) == 0
        assert "[dry-run] would write " in capsys.readouterr().out
