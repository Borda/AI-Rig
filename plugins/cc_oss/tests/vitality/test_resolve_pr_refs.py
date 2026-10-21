"""Tests for ``bin/resolve_pr_refs.py``.

Default-branch parsing and PR-metadata extraction are pure; the git and ``gh`` calls monkeypatch ``subprocess.run`` so
no real repository or network is touched. Sentinel assertions read back the files written under an isolated ``TMPDIR``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import resolve_pr_refs as rpr


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output consumed by the resolver."""
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def tmp_sentinels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point sentinel writes at an isolated temp directory."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


def _sentinel(tmp_path: Path, name: str) -> str:
    """Read one sentinel written by the script, without its trailing newline."""
    return (tmp_path / f"{name}-shared").read_text(encoding="utf-8").rstrip("\n")


def _wire(monkeypatch: pytest.MonkeyPatch, *, default_ref: str, pr_meta: dict | None, head: str = "abc123") -> None:
    """Answer the default-branch lookup, the PR fetch and the local SHA reads."""

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        if cmd[:2] == ["git", "symbolic-ref"]:
            return _FakeCompleted(stdout=default_ref) if default_ref else _FakeCompleted(returncode=1)
        if cmd[:2] == ["git", "remote"]:
            return _FakeCompleted(returncode=1)
        if cmd[:2] == ["git", "rev-parse"]:
            return _FakeCompleted(stdout="feature-branch" if "--abbrev-ref" in cmd else head)
        if cmd[0] == "gh":
            return _FakeCompleted(stdout=json.dumps(pr_meta)) if pr_meta is not None else _FakeCompleted(returncode=1)
        return _FakeCompleted(returncode=1)

    monkeypatch.setattr(rpr.subprocess, "run", fake_run)


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


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_parse_head_branch() -> None:
    """The default branch is lifted from the ``HEAD branch:`` line."""
    assert rpr.parse_head_branch("* remote origin\n  HEAD branch: develop\n  Remote branches:") == "develop"


def test_default_branch_prefers_local_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local symbolic ref answers without a network round trip."""
    _wire(monkeypatch, default_ref="refs/remotes/origin/main", pr_meta=None)
    assert rpr.default_branch(5) == "main"


def test_default_branch_falls_back_to_remote_show(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a local ref, ``git remote show origin`` is parsed instead."""

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        if cmd[:2] == ["git", "symbolic-ref"]:
            return _FakeCompleted(returncode=1)
        return _FakeCompleted(stdout="  HEAD branch: trunk")

    monkeypatch.setattr(rpr.subprocess, "run", fake_run)
    assert rpr.default_branch(5) == "trunk"


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        pytest.param({"headRepositoryOwner": {"login": "contrib"}}, "contrib", id="present"),
        pytest.param({"headRepositoryOwner": None}, "", id="null-owner"),
        pytest.param({}, "", id="absent"),
    ],
)
def test_head_repo_owner(meta: dict, expected: str) -> None:
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
def test_head_repo_name(meta: dict, expected: str) -> None:
    """The head repository's name is read defensively — a deleted fork reports no repository object.

    Step 10 hands the name to ``derive_fork_remote.py --fork-repo``; an empty value keeps its default of reusing the
    upstream name.
    """
    assert rpr.head_repo_name(meta) == expected


def test_fetch_pr_meta_requests_head_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single ``gh pr view`` call asks for ``headRepository``, the source of a renamed fork's name.

    Without the field the payload never carries the name, so a fork renamed after forking would silently get a push URL
    built from the upstream name.
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        calls.append(cmd)
        return _FakeCompleted(stdout="{}")

    monkeypatch.setattr(rpr.subprocess, "run", fake_run)
    rpr.fetch_pr_meta("42", 5)
    assert "headRepository" in calls[0][calls[0].index("--json") + 1].split(",")


def test_step10_push_block_passes_head_repo_name_to_derive_fork_remote() -> None:
    """``oss:resolve`` Step 10 reads the ``resolve-head-repo-name`` sentinel and hands it to ``--fork-repo``.

    The sentinel is useless unless the push block consumes it: a fork remote missing at push time would otherwise be
    added with the upstream repository name, pointing at a repository the contributor may not own.
    """
    skill = (Path(rpr.__file__).resolve().parents[1] / "skills" / "resolve" / "SKILL.md").read_text(encoding="utf-8")
    assert 'IFS= read -r FORK_REPO < "${TMPDIR:-/tmp}/resolve-head-repo-name-${CSID}"' in skill
    assert '--fork-repo "$FORK_REPO"' in skill


def test_fetch_pr_meta_returns_empty_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unusable ``gh`` response degrades to an empty payload rather than raising."""
    monkeypatch.setattr(rpr.subprocess, "run", lambda *_a, **_k: _FakeCompleted(stdout="not json"))
    assert rpr.fetch_pr_meta("42", 5) == {}


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


@pytest.mark.parametrize(("name", "valid"), _BRANCH_NAME_CASES)
def test_is_valid_branch_name(name: str, valid: bool) -> None:
    """Names git's branch rules reject, and names that would parse as an option, are refused.

    The PR author controls ``headRefName``/``baseRefName``; the value is later passed to ``git switch`` and ``gh pr
    checkout`` as a bare argument, so the check has to run before the value is persisted.
    """
    assert rpr.is_valid_branch_name(name) is valid


@pytest.mark.parametrize(("name", "valid"), _BRANCH_NAME_CASES)
def test_is_valid_branch_name_matches_git(tmp_path: Path, name: str, valid: bool) -> None:
    """The pure-Python check agrees with ``git check-ref-format --branch`` on every case.

    A hand-written copy of git's rules can drift from git; this runs the real command against the same inputs. The
    ``--branch`` form is the one that also refuses a leading ``-``.
    """
    proc = subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True, cwd=tmp_path, check=False)
    assert (proc.returncode == 0) is valid


# ---------------------------------------------------------------------------
# argv-injection guard
# ---------------------------------------------------------------------------


def test_main_rejects_leading_dash_pr(capsys: pytest.CaptureFixture) -> None:
    """A ``--pr`` value starting with '-' is refused before any subprocess runs.

    ``--pr`` flows raw into ``gh pr view <pr>`` argv; a leading-dash value would be consumed as a flag rather than an
    operand. Uses the ``--pr=<value>`` single-token form: argparse's own tokenizer treats a bare ``--pr --evil-flag``
    two-token pair as a *missing* value (unknown-option lookahead), which would exit 2 through argparse itself rather
    than exercising this guard.
    """
    assert rpr.main(["--pr=--evil-flag"]) == 1
    assert "must not start with" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# branch-safety pre-check
# ---------------------------------------------------------------------------


def test_main_persists_all_sentinels(tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A safe PR writes every ref the later steps reload."""
    _wire(monkeypatch, default_ref="refs/remotes/origin/main", pr_meta=_meta())
    assert rpr.main(["--pr", "42"]) == 0
    assert _sentinel(tmp_sentinels, "resolve-head-ref") == "feature-branch"
    assert _sentinel(tmp_sentinels, "resolve-base-ref") == "main"
    assert _sentinel(tmp_sentinels, "resolve-is-cross-repo") == "true"
    assert _sentinel(tmp_sentinels, "resolve-head-repo-owner") == "contributor"
    assert _sentinel(tmp_sentinels, "resolve-head-repo-name") == "renamed-fork"
    assert _sentinel(tmp_sentinels, "resolve-saved-branch") == "feature-branch"
    assert _sentinel(tmp_sentinels, "resolve-pr-head-oid") == "abc123"
    assert _sentinel(tmp_sentinels, "resolve-local-sha") == "abc123"


def test_main_blocks_when_head_is_default_branch(
    tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A PR whose head is the default branch is refused before any sentinel is written."""
    _wire(monkeypatch, default_ref="refs/remotes/origin/main", pr_meta=_meta(headRefName="main"))
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
def test_main_blocks_invalid_ref_names_before_persisting(
    tmp_sentinels: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    overrides: dict[str, str],
    label: str,
) -> None:
    """A head or base ref that is not a valid branch name is refused and no sentinel is written.

    Persisting it would hand an attacker-chosen string to ``git switch "$PR_HEAD_REF"`` later, where a leading dash is
    parsed as an option (``--detach`` detaches HEAD instead of switching).
    """
    _wire(monkeypatch, default_ref="refs/remotes/origin/main", pr_meta=_meta(**overrides))
    assert rpr.main(["--pr", "42"]) == 1
    assert f"PR {label} ref" in capsys.readouterr().out
    assert list(tmp_sentinels.glob("resolve-*")) == []


def test_main_blocks_without_default_branch(
    tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """An undeterminable default branch fails closed — the safety check cannot be evaluated."""
    _wire(monkeypatch, default_ref="", pr_meta=_meta())
    assert rpr.main(["--pr", "42"]) == 1
    assert "cannot determine default branch" in capsys.readouterr().out


def test_main_defaults_base_ref_and_cross_repo(tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed PR fetch still yields usable defaults: base ref from the repo, cross-repo false."""
    _wire(monkeypatch, default_ref="refs/remotes/origin/main", pr_meta=None)
    assert rpr.main(["--pr", "42"]) == 0
    assert _sentinel(tmp_sentinels, "resolve-base-ref") == "main"
    assert _sentinel(tmp_sentinels, "resolve-is-cross-repo") == "false"
    assert _sentinel(tmp_sentinels, "resolve-pr-head-oid") == ""


def test_main_emits_state_trace_on_stderr(
    tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The reflog trace keeps Step 4's opaque branch state visible."""
    _wire(monkeypatch, default_ref="refs/remotes/origin/main", pr_meta=_meta())
    rpr.main(["--pr", "42"])
    assert "→ Step 4 state: SAVED_BRANCH=feature-branch PR_HEAD_REF=feature-branch" in capsys.readouterr().err


def test_sentinel_has_trailing_newline(tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sentinels end in a newline — ``IFS= read -r`` fails on a file without one."""
    _wire(monkeypatch, default_ref="refs/remotes/origin/main", pr_meta=_meta())
    rpr.main(["--pr", "42"])
    assert (tmp_sentinels / "resolve-head-ref-shared").read_text(encoding="utf-8") == "feature-branch\n"


class TestLocalBase:
    """Covers --local-base: the no-PR path publishes only the default branch as the QA base."""

    def test_publishes_default_branch(self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A local origin/HEAD answers and lands in ``resolve-base-ref`` without any PR fetch.

        Report mode with no PR number and comment dispatch skip Step 4, so this is the only writer of the base that Step
        9's QA range reads.
        """
        _wire(monkeypatch, default_ref="refs/remotes/origin/trunk", pr_meta=None)
        assert rpr.main(["--local-base"]) == 0
        assert _sentinel(tmp_sentinels, "resolve-base-ref") == "trunk"
        assert not (tmp_sentinels / "resolve-head-ref-shared").exists()

    def test_falls_back_to_remote_show(self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without a local origin/HEAD the ``git remote show origin`` answer is used, as on the PR path."""

        def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
            if cmd[:2] == ["git", "remote"]:
                return _FakeCompleted(stdout="* remote origin\n  HEAD branch: develop\n")
            return _FakeCompleted(returncode=1)

        monkeypatch.setattr(rpr.subprocess, "run", fake_run)
        assert rpr.main(["--local-base"]) == 0
        assert _sentinel(tmp_sentinels, "resolve-base-ref") == "develop"

    def test_undeterminable_base_leaves_sentinel_empty(
        self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """No default branch exits 1 and clears a stale value so QA cannot reuse the previous run's base."""
        (tmp_sentinels / "resolve-base-ref-shared").write_text("stale\n", encoding="utf-8")
        _wire(monkeypatch, default_ref="", pr_meta=None)
        assert rpr.main(["--local-base"]) == 1
        assert "Cannot determine a valid origin default branch" in capsys.readouterr().out
        assert _sentinel(tmp_sentinels, "resolve-base-ref") == ""


class TestDryRun:
    """Covers --dry-run: compute and print, write no session state.

    These sentinels are live skill state keyed by ``CSID``, read by later steps and by PreToolUse hooks. An inspection
    run of this script must not forge them — one such run wrote an ``analyse-report-file`` naming a report that was
    never produced, and the hook denial that followed blocked an unrelated question.
    """

    def test_writes_no_sentinels(self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--dry-run leaves the sentinel directory empty and exits 0."""
        _wire(monkeypatch, default_ref="refs/remotes/origin/main", pr_meta=_meta())
        assert rpr.main(["--pr", "42", "--dry-run"]) == 0
        assert list(tmp_sentinels.glob("resolve-*")) == []

    def test_announces_what_it_would_write(
        self, tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Each suppressed write is still reported, so a verifier sees the computed value."""
        _wire(monkeypatch, default_ref="refs/remotes/origin/main", pr_meta=_meta())
        assert rpr.main(["--pr", "42", "--dry-run"]) == 0
        assert "[dry-run] would write " in capsys.readouterr().out
