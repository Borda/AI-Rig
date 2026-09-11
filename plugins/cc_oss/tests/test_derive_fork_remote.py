"""Tests for ``bin/derive_fork_remote.py``.

URL derivation is pure; every git interaction monkeypatches ``subprocess.run``, recording the commands issued so the
remote-add and upstream-tracking behaviour can be asserted without touching a real repository.
"""

from __future__ import annotations

import pytest

import derive_fork_remote as dfr


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output consumed by the caller."""
        self.returncode = returncode
        self.stdout = stdout


class _GitRecorder:
    """Answer git calls from a lookup table and record every invocation."""

    def __init__(self, responses: dict[str, _FakeCompleted]) -> None:
        """Store canned responses keyed by a distinctive argument of each command."""
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        """Record the command and return the first matching canned response."""
        self.calls.append(cmd)
        for key, response in self.responses.items():
            if key in cmd or any(key in part for part in cmd):
                return response
        return _FakeCompleted(returncode=1)


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        pytest.param("git@github.com:upstream/repo.git", "repo", id="ssh"),
        pytest.param("https://github.com/upstream/repo.git", "repo", id="https"),
        pytest.param("https://github.com/upstream/repo", "repo", id="https-no-suffix"),
    ],
)
def test_repo_name_from_origin(origin: str, expected: str) -> None:
    """The bare repository name is the last path segment without ``.git``."""
    assert dfr.repo_name_from_origin(origin) == expected


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        pytest.param("git@github.com:upstream/repo.git", "git@github.com:contrib/repo.git", id="ssh-mirrors-ssh"),
        pytest.param("https://github.com/upstream/repo.git", "https://github.com/contrib/repo.git", id="https"),
        pytest.param("", "https://github.com/contrib/repo.git", id="unknown-origin-defaults-to-https"),
    ],
)
def test_fork_url_mirrors_origin_transport(origin: str, expected: str) -> None:
    """The fork URL uses the same transport as origin — an SSH-only checkout has no HTTPS credentials."""
    assert dfr.fork_url(origin, "contrib", "repo") == expected


def test_ensure_fork_remote_skips_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-known remote is left alone."""
    recorder = _GitRecorder({"get-url": _FakeCompleted(stdout="git@github.com:contrib/repo.git")})
    monkeypatch.setattr(dfr.subprocess, "run", recorder)
    dfr.ensure_fork_remote("contrib", 5)
    assert not any("add" in cmd for cmd in recorder.calls)


def test_ensure_fork_remote_adds_when_missing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A missing remote is added with a transport-matched URL and announced."""

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        if cmd[:3] == ["git", "remote", "get-url"] and cmd[3] == "contrib":
            return _FakeCompleted(returncode=1)
        if cmd[:3] == ["git", "remote", "get-url"]:
            return _FakeCompleted(stdout="git@github.com:upstream/repo.git")
        return _FakeCompleted()

    monkeypatch.setattr(dfr.subprocess, "run", fake_run)
    dfr.ensure_fork_remote("contrib", 5)
    assert "→ Added remote contrib → git@github.com:contrib/repo.git" in capsys.readouterr().out


def test_push_scope_uses_fork_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the fork branch exists, the scope is measured against it."""

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        if "rev-list" in cmd:
            return _FakeCompleted(stdout="3")
        return _FakeCompleted(stdout=" file.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)")

    monkeypatch.setattr(dfr.subprocess, "run", fake_run)
    count, stat = dfr.push_scope("contrib", "feature", "main", 5)
    assert count == "3"
    assert stat == "1 file changed, 1 insertion(+), 1 deletion(-)"


def test_push_scope_falls_back_to_base_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """A branch not yet on the fork is measured against the base branch instead."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        calls.append(cmd)
        if "contrib/feature..HEAD" in cmd:
            return _FakeCompleted(returncode=1)
        if "rev-list" in cmd:
            return _FakeCompleted(stdout="5")
        return _FakeCompleted(stdout=" 2 files changed, 9 insertions(+)")

    monkeypatch.setattr(dfr.subprocess, "run", fake_run)
    count, stat = dfr.push_scope("contrib", "feature", "main", 5)
    assert count == "5"
    assert stat == "2 files changed, 9 insertions(+)"
    assert any("origin/main..HEAD" in cmd for cmd in calls)


@pytest.mark.parametrize(
    ("fork_remote", "head_ref"),
    [pytest.param("", "feature", id="no-remote"), pytest.param("contrib", "", id="no-head-ref")],
)
def test_main_blocks_on_unresolved_refs(fork_remote: str, head_ref: str, capsys: pytest.CaptureFixture) -> None:
    """An unresolved remote or head ref refuses to raise an empty push-authorization prompt."""
    assert dfr.main(["--fork-remote", fork_remote, "--head-ref", head_ref, "--base-ref", "main"]) == 1
    assert "FORK_REMOTE/HEAD_REF unresolved" in capsys.readouterr().out


def test_main_blocks_when_scope_uncomputable(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """With no commit count, the run stops instead of prompting for a push it cannot describe."""
    monkeypatch.setattr(dfr.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=1))
    assert dfr.main(["--fork-remote", "contrib", "--head-ref", "feature", "--base-ref", "main"]) == 1
    assert "push scope could not be computed" in capsys.readouterr().out


def test_main_prints_push_summary(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The success path prints the exact line the push-authorization question quotes."""

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        if "rev-list" in cmd:
            return _FakeCompleted(stdout="2")
        if "diff" in cmd:
            return _FakeCompleted(stdout=" 3 files changed, 47 insertions(+), 12 deletions(-)")
        if "log" in cmd:
            return _FakeCompleted(stdout="fix(core): guard empty input")
        return _FakeCompleted()

    monkeypatch.setattr(dfr.subprocess, "run", fake_run)
    assert dfr.main(["--fork-remote", "contrib", "--head-ref", "feature", "--base-ref", "main"]) == 0
    expected = (
        "→ 2 commits ready to push to contrib/feature (3 files changed, 47 insertions(+), 12 deletions(-)); "
        'last commit: "fix(core): guard empty input"'
    )
    assert expected in capsys.readouterr().out
