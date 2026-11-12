"""Real Git regressions for disabled hooks, explicit approval and bounded formatter recovery."""

from __future__ import annotations

import importlib
import shlex
import sys
from pathlib import Path
from typing import Any

import commit_action_item
import commit_all_items
import commit_lint_fixes
import pytest


@pytest.fixture
def hook_repo(scratch_repo: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Prepare one changed branch file and an executable Python-backed repository hook."""
    scratch_repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
    (scratch_repo.path / "selected.txt").write_text("before\n", encoding="utf-8")
    scratch_repo.git("add", "selected.txt")
    scratch_repo.git("commit", "-q", "-m", "branch file")
    (scratch_repo.path / "selected.txt").write_text("unformatted\n", encoding="utf-8")
    hooks = scratch_repo.path / ".githooks"
    hooks.mkdir()
    payload = hooks / "payload.py"
    payload.write_text("from pathlib import Path\nPath('ran-hook').write_text('yes')\n", encoding="utf-8")
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\nexec " + shlex.join([sys.executable.replace("\\", "/"), payload.as_posix()]) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)
    scratch_repo.git("config", "core.hooksPath", ".githooks")
    monkeypatch.delenv("RESOLVE_APPROVED_HOOKS", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "resolve-base-ref-shared").write_text("main\n", encoding="utf-8")
    return scratch_repo


def _commit(kind: str, repo: Any) -> int:
    """Invoke the three existing commit entrypoints with their real workflow arguments."""
    if kind == "all":
        repo.git("add", "selected.txt")
        return commit_all_items.main(["#7", "1", "0", "0"])
    if kind == "lint":
        return commit_lint_fixes.main([])
    message = repo.path.parent / "message.txt"
    message.write_text("format selected\n", encoding="utf-8")
    return commit_action_item.main(["--message-file", str(message), "--files", "selected.txt"])


def _approve(repo: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Approve the exact inspected hook snapshot through the public environment interface."""
    hooks = importlib.import_module("resolve_commit_hooks")
    report = hooks.inspect_repository_hooks("git")
    monkeypatch.setenv("RESOLVE_APPROVED_HOOKS", report["sha256"])
    return report


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["item", "all", "lint"])
def test_repository_hooks_disabled_by_default(hook_repo: Any, kind: str) -> None:
    """Each commit helper creates its commit without executing the configured repository hook."""
    assert _commit(kind, hook_repo) == 0
    assert not (hook_repo.path / "ran-hook").exists()
    assert hook_repo.git("show", "HEAD:selected.txt") == "unformatted\n"


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["item", "all", "lint"])
def test_approved_formatter_restages_only_selected_files_and_retries_once(
    hook_repo: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A formatter's first rejection is recovered while an unrelated staged edit stays separate."""
    (hook_repo.path / ".githooks/payload.py").write_text(
        "from pathlib import Path\n"
        "p = Path('selected.txt')\n"
        "if p.read_text() != 'formatted\\n':\n"
        "    p.write_text('formatted\\n')\n"
        "    raise SystemExit(1)\n",
        encoding="utf-8",
    )
    (hook_repo.path / "base.txt").write_text("unrelated\n", encoding="utf-8")
    _approve(hook_repo, monkeypatch)
    assert _commit(kind, hook_repo) == 0
    assert hook_repo.git("show", "HEAD:selected.txt") == "formatted\n"
    assert hook_repo.git("show", "HEAD:base.txt") == "base\n"
    assert hook_repo.git("diff", "--name-only") == "base.txt\n"


@pytest.mark.integration
def test_second_formatter_failure_is_reported_without_third_attempt(
    hook_repo: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hook that changes the selected file on every attempt stops after exactly two executions."""
    (hook_repo.path / ".githooks/payload.py").write_text(
        "from pathlib import Path\np = Path('selected.txt')\np.write_text(p.read_text() + 'again\\n')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    head = hook_repo.git("rev-parse", "HEAD")
    _approve(hook_repo, monkeypatch)
    assert _commit("item", hook_repo) == 1
    assert hook_repo.git("rev-parse", "HEAD") == head
    assert (hook_repo.path / "selected.txt").read_text() == "unformatted\nagain\nagain\n"
    assert "second" in capsys.readouterr().err


@pytest.mark.integration
def test_hook_staging_unrelated_dirty_file_prevents_retry(hook_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook cannot add an already-dirty unrelated file to the bulk recovery commit through index changes alone."""
    (hook_repo.path / "base.txt").write_text("unrelated\n", encoding="utf-8")
    (hook_repo.path / ".githooks/payload.py").write_text(
        "from pathlib import Path\nimport subprocess\n"
        "subprocess.run(['git', 'add', 'base.txt'], check=True)\n"
        "p = Path('selected.txt')\n"
        "if p.read_text() != 'formatted\\n':\n"
        "    p.write_text('formatted\\n')\n"
        "    raise SystemExit(1)\n",
        encoding="utf-8",
    )
    before = hook_repo.git("rev-parse", "HEAD")
    _approve(hook_repo, monkeypatch)
    assert _commit("all", hook_repo) == 1
    assert hook_repo.git("rev-parse", "HEAD") == before
    assert "base.txt" in hook_repo.git("diff", "--cached", "--name-only")


@pytest.mark.integration
@pytest.mark.parametrize("outside", ["base.txt", "new-untracked.txt"])
def test_hook_changes_outside_selection_prevent_retry(
    hook_repo: Any, monkeypatch: pytest.MonkeyPatch, outside: str
) -> None:
    """An approved hook cannot cause recovery to stage a modified unrelated or new untracked file."""
    (hook_repo.path / ".githooks/payload.py").write_text(
        f"from pathlib import Path\nPath({outside!r}).write_text('hook edit')\n"
        "p = Path('selected.txt')\np.write_text(p.read_text() + 'again\\n')\nraise SystemExit(1)\n",
        encoding="utf-8",
    )
    _approve(hook_repo, monkeypatch)
    assert _commit("item", hook_repo) == 1
    assert (hook_repo.path / "selected.txt").read_text() == "unformatted\nagain\n"
    assert hook_repo.git("diff", "--cached", "--name-only") == "selected.txt\n"


@pytest.mark.integration
def test_changed_hook_configuration_invalidates_approval(hook_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Editing a hook after its exact contents were displayed requires a new approval."""
    report = _approve(hook_repo, monkeypatch)
    assert any("Path('ran-hook')" in entry.get("content", "") for entry in report["files"])
    (hook_repo.path / ".githooks/payload.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    head = hook_repo.git("rev-parse", "HEAD")
    assert _commit("item", hook_repo) == 1
    assert hook_repo.git("rev-parse", "HEAD") == head


@pytest.mark.integration
def test_failed_hook_without_file_changes_is_not_retried(hook_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A validation failure without formatter edits returns immediately rather than rerunning hooks."""
    counter = hook_repo.path.parent / "attempts.txt"
    (hook_repo.path / ".githooks/payload.py").write_text(
        f"from pathlib import Path\np = Path({str(counter)!r})\n"
        "p.write_text((p.read_text() if p.exists() else '') + 'run\\n')\nraise SystemExit(2)\n",
        encoding="utf-8",
    )
    _approve(hook_repo, monkeypatch)
    assert _commit("item", hook_repo) == 1
    assert counter.read_text() == "run\n"


@pytest.mark.integration
def test_hook_deletion_outside_selection_prevents_retry(hook_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting a previously clean unrelated file is detected even though its post-hook content is absent."""
    (hook_repo.path / ".githooks/payload.py").write_text(
        "from pathlib import Path\nPath('base.txt').unlink(missing_ok=True)\n"
        "p = Path('selected.txt')\np.write_text(p.read_text() + 'again\\n')\nraise SystemExit(1)\n",
        encoding="utf-8",
    )
    _approve(hook_repo, monkeypatch)
    assert _commit("item", hook_repo) == 1
    assert (hook_repo.path / "selected.txt").read_text() == "unformatted\nagain\n"
    assert hook_repo.git("diff", "--cached", "--name-only") == "selected.txt\n"


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["item", "all", "lint"])
def test_formatter_can_delete_selected_file(hook_repo: Any, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """A selected-file deletion is restaged and committed on the bounded second attempt."""
    (hook_repo.path / ".githooks/payload.py").write_text(
        "from pathlib import Path\np = Path('selected.txt')\nif p.exists():\n    p.unlink()\n    raise SystemExit(1)\n",
        encoding="utf-8",
    )
    _approve(hook_repo, monkeypatch)
    assert _commit(kind, hook_repo) == 0
    assert hook_repo.git("ls-tree", "--name-only", "HEAD", "selected.txt") == ""


@pytest.mark.integration
def test_hook_edit_to_already_dirty_unrelated_file_prevents_retry(
    hook_repo: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook edit to an already dirty file cannot hide behind its unchanged Git status code."""
    (hook_repo.path / "base.txt").write_text("existing local edit\n", encoding="utf-8")
    (hook_repo.path / ".githooks/payload.py").write_text(
        "from pathlib import Path\nPath('base.txt').write_text('hook edit')\n"
        "p = Path('selected.txt')\np.write_text(p.read_text() + 'again\\n')\nraise SystemExit(1)\n",
        encoding="utf-8",
    )
    _approve(hook_repo, monkeypatch)
    assert _commit("item", hook_repo) == 1
    assert (hook_repo.path / "selected.txt").read_text() == "unformatted\nagain\n"
    assert hook_repo.git("diff", "--cached", "--name-only") == "selected.txt\n"


@pytest.mark.integration
def test_repository_hook_config_changes_invalidate_approval(hook_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-commit configuration introduced after inspection cannot reuse an earlier hook approval."""
    _approve(hook_repo, monkeypatch)
    (hook_repo.path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    head = hook_repo.git("rev-parse", "HEAD")
    assert _commit("item", hook_repo) == 1
    assert hook_repo.git("rev-parse", "HEAD") == head
    assert not (hook_repo.path / "ran-hook").exists()


@pytest.mark.integration
def test_merge_commit_route_requires_active_merge(hook_repo: Any) -> None:
    """The dedicated merge route cannot commit ordinary staged changes when no merge is active."""
    hooks = importlib.import_module("resolve_commit_hooks")
    head = hook_repo.git("rev-parse", "HEAD")
    assert hooks.main(["--commit-merge"]) == 1
    assert hook_repo.git("rev-parse", "HEAD") == head


@pytest.mark.integration
def test_merge_commit_route_disables_unapproved_hooks(hook_repo: Any) -> None:
    """The merge CLI commits an active merge without executing its unapproved repository hooks."""
    hooks = importlib.import_module("resolve_commit_hooks")
    empty = hook_repo.path.parent / "empty-hooks"
    empty.mkdir()
    hook_repo.git("checkout", "--", "selected.txt")
    hook_repo.git("switch", "-q", "-c", "side", "HEAD~1")
    (hook_repo.path / "side.txt").write_text("side\n", encoding="utf-8")
    hook_repo.git("add", "side.txt")
    hook_repo.git("-c", f"core.hooksPath={empty}", "commit", "-q", "-m", "side")
    hook_repo.git("switch", "-q", "main")
    hook_repo.git("merge", "--no-commit", "side")
    assert hooks.main(["--commit-merge"]) == 0
    assert len(hook_repo.git("rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    assert not (hook_repo.path / "ran-hook").exists()
