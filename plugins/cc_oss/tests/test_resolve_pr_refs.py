"""Tests for ``bin/resolve_pr_refs.py``.

Default-branch parsing and PR-metadata extraction are pure; the git and ``gh`` calls monkeypatch ``subprocess.run`` so
no real repository or network is touched. Sentinel assertions read back the files written under an isolated ``TMPDIR``.
"""

from __future__ import annotations

import json
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


def test_fetch_pr_meta_returns_empty_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unusable ``gh`` response degrades to an empty payload rather than raising."""
    monkeypatch.setattr(rpr.subprocess, "run", lambda *_a, **_k: _FakeCompleted(stdout="not json"))
    assert rpr.fetch_pr_meta("42", 5) == {}


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
