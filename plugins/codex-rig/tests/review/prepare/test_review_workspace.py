"""Exercise caller selection and read-only discovery of historical PR review repositories."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from _git_fixtures import git_text, historical_review_workspace
from _modules import load_module
from _platform import DIRECTORY_SYMLINKS_AVAILABLE

SHARED = Path(__file__).resolve().parents[3] / "shared"
TARGET = "https://github.com/acme/widgets/pull/17"


@pytest.fixture
def caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Prepare a checkout with a matching upstream while origin identifies a fork."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path.resolve() / "state"))
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(tmp_path.resolve()))
    repo = tmp_path.resolve() / "caller"
    repo.mkdir()
    git_text(repo, "init", "-q")
    git_text(repo, "remote", "add", "origin", "https://github.com/fork/widgets")
    git_text(repo, "remote", "add", "upstream", "git@github.com:Acme/Widgets.git")
    return repo


def prepare(checkout: Path, target: str = TARGET, *, success: bool = True) -> Path | None:
    """Execute the public preparation CLI and require its expected success or refusal."""
    result = subprocess.run(
        [sys.executable, "-B", str(SHARED / "review_workspace.py"), "--checkout", str(checkout), "--target", target],
        capture_output=True,
        text=True,
        check=False,
    )
    if success:
        assert result.returncode == 0, result.stderr
        return Path(result.stdout.strip())
    assert result.returncode != 0
    assert not result.stdout.strip()
    return None


@pytest.mark.integration
class TestReviewCaller:
    """Fresh entry resolves the caller, while historical evidence retains its independently verified owner."""

    def test_fresh_entry_uses_caller_without_publishing_state(self, caller: Path) -> None:
        """Different PRs and repeated calls from a subdirectory keep Git configuration and storage unchanged."""
        before = (caller / ".git/config").read_bytes()
        nested = caller / "nested"
        nested.mkdir()
        assert prepare(nested) == caller
        assert prepare(caller, TARGET.replace("/17", "/18")) == caller
        assert not (caller / ".git/codex-rig").exists()
        assert not (caller / ".git/codex-review-locations").exists()
        assert not Path(os.environ["XDG_STATE_HOME"]).exists()
        assert (caller / ".git/config").read_bytes() == before

    def test_historical_entry_resolves_original_after_override_changes(
        self, caller: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A historical run remains bound to its original caller even after relocating the state override."""
        module = load_module("retained_review", SHARED / "review_workspace.py")
        historical = historical_review_workspace(caller, TARGET)
        monkeypatch.setenv("XDG_STATE_HOME", str(caller.parent / "new-state"))
        assert module.workspace_caller(historical, TARGET) == caller
        assert prepare(historical) == caller
        assert prepare(caller) == caller
        assert not Path(os.environ["XDG_STATE_HOME"]).exists()
        with pytest.raises(ValueError, match="identity mismatch"):
            module.workspace_caller(historical, TARGET.replace("/17", "/18"))

    def test_lookup_preserves_a_historical_workspace_without_location_receipt(
        self, caller: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The original Git-local layout remains readable without writing a replacement location receipt."""
        monkeypatch.delenv("XDG_STATE_HOME")
        historical = historical_review_workspace(caller, TARGET)
        receipt = caller / ".git" / "codex-review-locations" / historical.name / "location"
        receipt.unlink()
        module = load_module("unregistered_history", SHARED / "review_workspace.py")
        assert module.historical_workspace(caller, TARGET) == historical
        assert module.workspace_caller(historical, TARGET) == caller
        assert prepare(historical) == caller
        assert not receipt.exists()

    def test_linked_entry_resolves_registered_caller(self, caller: Path) -> None:
        """A caller that is itself a linked worktree remains the source, including a reciprocal registration check."""
        git_text(
            caller,
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        )
        linked = caller.parent / "linked"
        git_text(caller, "worktree", "add", "--detach", str(linked))
        assert prepare(linked) == linked
        own = Path(git_text(linked, "rev-parse", "--absolute-git-dir"))
        (own / "gitdir").write_text(str(caller / ".git"), encoding="utf-8")
        assert prepare(linked, success=False) is None
        assert not (own / "codex-rig").exists()

    @pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="directory symlink capability required")
    def test_redirected_historical_workspace_is_refused(self, caller: Path) -> None:
        """An alias cannot redirect historical artifact discovery into a different repository."""
        module = load_module("retained_identity", SHARED / "review_workspace.py")
        workspace = historical_review_workspace(caller, TARGET)
        other = workspace.with_name("moved")
        workspace.rename(other)
        workspace.symlink_to(other, target_is_directory=True)
        with pytest.raises(ValueError, match="redirected"):
            module.workspace_caller(workspace, TARGET)

    @pytest.mark.parametrize("damage", ["missing binding", "duplicate caller", "alternate objects", "common directory"])
    def test_damaged_private_namespace_cannot_be_treated_as_ordinary(self, caller: Path, damage: str) -> None:
        """Invalid bindings or shared-storage redirects prevent reuse of a historical repository."""
        module = load_module("private_workspace_corruption", SHARED / "review_workspace.py")
        workspace = historical_review_workspace(caller, TARGET)
        if damage == "missing binding":
            git_text(workspace, "config", "--local", "--unset", "codexRig.reviewTarget")
        elif damage == "duplicate caller":
            git_text(workspace, "config", "--local", "--add", "codexRig.reviewCaller", str(caller))
        elif damage == "alternate objects":
            (workspace / ".git/objects/info").mkdir(exist_ok=True)
            (workspace / ".git/objects/info/alternates").write_text(str(caller / ".git/objects"), encoding="utf-8")
        else:
            (workspace / ".git/commondir").write_text(str(caller / ".git"), encoding="utf-8")
        message = "identity" if damage in {"missing binding", "duplicate caller"} else "share Git"
        with pytest.raises(ValueError, match=message):
            module.workspace_caller(workspace, TARGET)
        assert prepare(caller) == caller

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            pytest.param("ssh://git@github.com/Acme/Widgets.git", "git@github.com:acme/widgets.git", id="ssh"),
            pytest.param("https://github.com/Acme/Widgets", "https://github.com/acme/widgets.git", id="https"),
            pytest.param(
                "https://name:sensitive-marker@github.com/Acme/Widgets",
                "https://github.com/acme/widgets.git",
                id="redacted-https",
            ),
            pytest.param("ssh://sensitive-marker@github.com/acme/widgets.git", None, id="unknown-ssh-user"),
            pytest.param("ssh://git@github.com:8022/acme/widgets.git", None, id="noncanonical-port"),
            pytest.param("file://github.com/acme/widgets.git", None, id="unsupported-transport"),
            pytest.param("https://github.com/acme/widgets/extra", None, id="extra-path"),
        ],
    )
    def test_transport_uses_only_sanitized_admitted_origin(self, caller: Path, url: str, expected: str | None) -> None:
        """Select a validated, credential-free transport without changing the configured caller remote."""
        git_text(caller, "remote", "set-url", "upstream", url)
        result = subprocess.run(
            [sys.executable, "-B", str(SHARED / "review_workspace.py"), "--checkout", str(caller), "--target", TARGET],
            capture_output=True,
            text=True,
            check=False,
        )
        assert "sensitive-marker" not in result.stderr + result.stdout
        if expected is None:
            assert result.returncode != 0
            assert not Path(os.environ["XDG_STATE_HOME"]).exists()
        else:
            assert result.returncode == 0, result.stderr
            assert Path(result.stdout.strip()) == caller
            module = load_module("review_transport", SHARED / "review_workspace.py")
            assert module.caller_fetch_url(caller, TARGET) == expected
            assert git_text(caller, "config", "--local", "--get-all", "remote.upstream.url") == url
