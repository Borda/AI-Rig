"""Subprocess tests for ``hooks/write-guard.js``.

The hook is a ``PreToolUse`` gate on ``Edit``/``Write``/``NotebookEdit`` that forces a
confirmation on every write to a small, repository-independent protected set
(agent instructions, Claude's own permission config, the pre-commit gate and package
metadata) until git tracks the file. Its contract:

* **Guard, never grant** — the only decision it can emit is ``ask``. It ships the
  PROTECT list rather than an ALLOW list precisely because an Edit carries no
  provenance: auto-allowing writes by directory convention would be
  ``acceptEdits`` with worse visibility.
* **Anchored matching** — a path merely containing a protected name
  (``src/my_changelog_helper.py``) is routine work and must pass through. A guard
  that fires on near-misses gets disabled by its user, which protects nothing.
* **Separator-independent** — patterns assume ``/`` and the input is normalized
  first, so a Windows host's backslashes cannot silently disable the guard.
* **Fail open** — a malformed payload, an unguarded tool, or a missing path
  produces empty stdout and exit 0, never a block.
* **Version-controlled files pass through** — a protected file git already
  tracks is diffable and revertible, so it is never gated, permission config and
  the commit gate included; untracked, new and gitignored protected files ask.
* **Session-created files ask once** — a missing protected file asks on creation and
  is then recorded per session, so later writes to it pass; a file that already
  existed untracked keeps asking.
* **Narrow protected set** — CI definitions and scripts under ``.github/``, release
  changelogs and dependency lockfiles are routine work and never ask, tracked or not.

Every case drives the hook over stdin, which is the path the harness itself uses,
rather than calling ``decide()`` in process — the JSON envelope shape is part of
the contract and an in-process call would not exercise it. Each run uses a
temporary directory as its session cwd, with git discovery capped above it, so
this repository's own tracked ``CLAUDE.md`` or ``CHANGELOG.md`` cannot turn an
expected ask into a passthrough.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent.parent / "hooks" / "write-guard.js"

NODE_UNAVAILABLE = shutil.which("node") is None
GIT_UNAVAILABLE = shutil.which("git") is None


@pytest.fixture(name="run_guard")
def _run_guard(tmp_path: Path) -> Callable[..., dict]:
    """Return a callable that feeds one payload to the hook and parses its stdout."""
    scratch = tmp_path.parent / f"{tmp_path.name}-tmp"
    scratch.mkdir()
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path.parent), "TMPDIR": str(scratch)}

    def _run(
        path: str,
        *,
        tool_name: str = "Edit",
        key: str = "file_path",
        cwd: Path = tmp_path,
        session_id: str = "session-a",
    ) -> dict:
        """Run the hook with one write-shaped payload and parse its result."""
        payload = json.dumps(
            {"tool_name": tool_name, "tool_input": {key: path}, "cwd": str(cwd), "session_id": session_id}
        )
        proc = subprocess.run(
            ["node", str(HOOK)],
            input=payload,
            capture_output=True,
            encoding="utf-8",
            timeout=10,
            cwd=cwd,
            env=env,
        )
        assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
        out = (proc.stdout or "").strip()
        return json.loads(out) if out else {}

    return _run


def _asks(result: dict) -> bool:
    """Check whether the hook requested user confirmation.

    Examples:
        >>> (_asks({"hookSpecificOutput": {"permissionDecision": "ask"}}), _asks({}))
        (True, False)
    """
    try:
        return result["hookSpecificOutput"]["permissionDecision"] == "ask"
    except (KeyError, TypeError):
        return False


# ── Protected files raise a confirmation ──────────────────────────────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node to execute the hook",
)


@_skip_node_unavailable
class TestProtectedPaths:
    """A write to a file in the protected set is gated behind a confirmation."""

    @pytest.mark.parametrize(
        ("path", "why"),
        [
            pytest.param("CLAUDE.md", "agent instructions", id="claude-md"),
            pytest.param("docs/nested/AGENTS.md", "agent instructions", id="nested-agents-md"),
            pytest.param(".claude/settings.json", "Claude permission config", id="permission-config"),
            pytest.param(".claude/settings.local.json", "Claude permission config", id="local-permission-config"),
            pytest.param(".pre-commit-config.yaml", "lint/format gate", id="lint-gate"),
            pytest.param("pyproject.toml", "package/release metadata", id="package-metadata"),
        ],
    )
    def test_each_protected_class(self, run_guard: Callable[..., dict], path: str, why: str) -> None:
        """Every protected class asks, and names the class it matched.

        The reason string is what the user reads in the confirmation dialog, so an
        unlabelled or mislabelled ask is a real defect: it turns a considered gate
        into unexplained friction, which is the state that gets guards removed.
        """
        result = run_guard(path)
        assert _asks(result), f"{path!r} should ask, got: {result}"
        assert (
            result["hookSpecificOutput"]["permissionDecisionReason"] == f"protected file ({why}) — confirm this write"
        )

    @pytest.mark.parametrize(
        "path",
        [
            # Tools may hand over either form depending on how the model addressed the file; a guard matching only
            # relative paths would be trivially sidestepped by an absolute one.
            pytest.param("/abs/repo/pyproject.toml", id="absolute-path"),
            # Patterns are written for ``/`` and the input is normalized first. Without that step the entire guard would
            # silently disappear on one OS while every macOS and Linux test stayed green — the project's recurrent
            # defect class.
            pytest.param("C:\\repo\\.claude\\settings.local.json", id="simulated-windows-separators"),
            # macOS and Windows filesystems are case-folding, so a write addressed as ``claude.md`` lands in the
            # real ``CLAUDE.md``. Under case-sensitive matching that path classifies as unprotected — and
            # passthrough means auto-approved whenever the hook is paired with ``acceptEdits``, which is the
            # configuration it exists for. This is the bypass, not a nicety.
            pytest.param("Claude.md", id="case-variant-claude-md"),
            pytest.param("PYPROJECT.TOML", id="case-variant-pyproject"),
            pytest.param(".Claude/Settings.json", id="case-variant-settings"),
        ],
    )
    def test_path_form_variants_still_ask(self, run_guard: Callable[..., dict], path: str) -> None:
        """A protected file addressed by another spelling of its path still asks.

        Scenario: an absolute path matches the same as a repository-relative one; a Windows-style path with backslashes
        still matches; case variants of a protected name still ask on case-folding filesystems.
        """
        assert _asks(run_guard(path)), f"{path!r} should ask — path-form bypass"

    @pytest.mark.parametrize(
        ("tool_name", "key"),
        [
            pytest.param("Write", "file_path", id="write-tool"),
            pytest.param("NotebookEdit", "notebook_path", id="notebookedit-notebook-path"),
        ],
    )
    def test_other_guarded_tools(self, run_guard: Callable[..., dict], tool_name: str, key: str) -> None:
        """Guard every supported file-editing operation consistently.

        ``Edit`` and ``Write`` are distinct matchers, so guarding only ``Edit`` would leave whole-file replacement open.
        ``NotebookEdit`` names its target ``notebook_path`` rather than ``file_path``; reading only the latter would
        ship that matcher registered but permanently inert.
        """
        assert _asks(run_guard("CLAUDE.md", tool_name=tool_name, key=key))


# ── Everything else is silent passthrough ─────────────────────────────────────


@_skip_node_unavailable
class TestPassthrough:
    """Routine work, near-misses and malformed input never reach a confirmation."""

    @pytest.mark.parametrize(
        "path",
        [
            "tests/training/callbacks/test_coco_eval_callback.py",
            "src/rfdetr/models/backbone.py",
            "tests/new_test_file.py",
        ],
    )
    def test_routine_work(self, run_guard: Callable[..., dict], path: str) -> None:
        """Source and test files are deliberately unprotected — that is the routine work.

        Protecting them would reintroduce exactly the prompt-per-edit friction this hook exists to let ``acceptEdits``
        remove.
        """
        assert run_guard(path) == {}

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param(".github/workflows/ci.yml", id="ci-workflow"),
            pytest.param(".github/scripts/action_runner.py", id="ci-helper-script"),
            pytest.param("CHANGELOG.md", id="changelog"),
            pytest.param("uv.lock", id="uv-lock"),
            pytest.param("pnpm-lock.yaml", id="pnpm-lock"),
            pytest.param("package-lock.json", id="package-lock"),
        ],
    )
    def test_formerly_protected_files(self, run_guard: Callable[..., dict], path: str) -> None:
        """CI files, changelogs and lockfiles never ask, tracked or not.

        A new file is untracked by nature, so asking on it harassed every edit of work the user had just asked for while
        protecting nothing: the write is visible in ``git status`` and revertible the moment it is committed.
        """
        assert run_guard(path) == {}, f"{path!r} asked — no longer protected"

    @pytest.mark.parametrize("path", ["src/my_changelog_helper.py", "src/github/client.py", "docs/pyproject_notes.md"])
    def test_near_misses(self, run_guard: Callable[..., dict], path: str) -> None:
        """A path merely containing a protected name is not protected.

        Substring matching would catch all three of these. Patterns are anchored on a path-separator boundary so the
        guard stays narrow enough to keep.
        """
        assert run_guard(path) == {}, f"{path!r} asked — guard is too broad"

    @pytest.mark.parametrize("tool_name", ["Read", "Bash", "Glob"])
    def test_unguarded_tools(self, run_guard: Callable[..., dict], tool_name: str) -> None:
        """Non-writing tools are ignored even when the path would match.

        Reading ``CLAUDE.md`` costs nothing; only a write does. Gating reads would make the guard fire constantly and
        teach the user to click through it.
        """
        assert run_guard("CLAUDE.md", tool_name=tool_name) == {}

    def test_missing_path(self, run_guard: Callable[..., dict]) -> None:
        """A guarded tool call carrying no path passes through rather than asking."""
        assert run_guard("", tool_name="Edit") == {}

    def test_malformed_stdin(self) -> None:
        """Malformed stdin never crashes or blocks.

        A hook that exits non-zero or throws on unexpected input would break every write in the session, which is a far
        worse failure than missing a guard.
        """
        proc = subprocess.run(
            ["node", str(HOOK)],
            input="{not json",
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )
        assert proc.returncode == 0
        assert (proc.stdout or "").strip() == ""


# ── Version-controlled protected files ────────────────────────────────────────


@pytest.fixture(name="git_repo")
def _git_repo(tmp_path: Path) -> Path:
    """Build a repository tracking four protected files, with two more present on disk but not tracked.

    ``CLAUDE.md``, ``pyproject.toml``, ``.claude/settings.json`` and ``.pre-commit-config.yaml`` are tracked.
    ``docs/AGENTS.md`` exists but is untracked, and ``.claude/settings.local.json`` is gitignored.
    """
    git = ["git", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q"], check=True, timeout=30)
    (tmp_path / "docs").mkdir()
    (tmp_path / ".claude").mkdir()
    tracked = ("CLAUDE.md", "pyproject.toml", ".claude/settings.json", ".pre-commit-config.yaml")
    for rel in (*tracked, "docs/AGENTS.md", ".claude/settings.local.json"):
        (tmp_path / rel).write_text("x\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".claude/settings.local.json\n", encoding="utf-8")
    subprocess.run([*git, "add", *tracked, ".gitignore"], check=True, timeout=30)
    return tmp_path


@_skip_node_unavailable
@pytest.mark.skipif(GIT_UNAVAILABLE, reason="requires git to build a repository")
class TestVersionControlled:
    """A protected file git tracks always passes through; one git cannot revert asks."""

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("CLAUDE.md", id="agent-instructions"),
            pytest.param("pyproject.toml", id="package-metadata"),
            pytest.param(".claude/settings.json", id="permission-config"),
            pytest.param(".pre-commit-config.yaml", id="lint-gate"),
        ],
    )
    def test_tracked_file_passes_through(self, run_guard: Callable[..., dict], git_repo: Path, path: str) -> None:
        """A tracked protected file is never gated, permission config and the commit gate included.

        Every write to it lands in ``git diff`` and can be reverted, which is the review the confirmation stood in for.
        Asking anyway produced a per-edit prompt the user could never dismiss, since a hook ``ask`` is not remembered.
        """
        assert run_guard(path, cwd=git_repo) == {}

    def test_tracked_file_by_absolute_path_passes_through(self, run_guard: Callable[..., dict], git_repo: Path) -> None:
        """An absolute path to a tracked file resolves against the repository that owns it."""
        assert run_guard(str(git_repo / "CLAUDE.md"), cwd=git_repo) == {}

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("docs/AGENTS.md", id="untracked-existing"),
            pytest.param("AGENTS.md", id="new-file"),
            pytest.param(".claude/settings.local.json", id="gitignored"),
        ],
    )
    def test_untracked_file_still_asks(self, run_guard: Callable[..., dict], git_repo: Path, path: str) -> None:
        """A protected file git does not track still asks, even inside a repository.

        Nothing records or reverts a write to an untracked, new or gitignored file, so the confirmation is the only
        review it gets. ``settings.local.json`` is the case that matters most: it is gitignored by convention.
        """
        assert _asks(run_guard(path, cwd=git_repo))


# ── Files the running session created ─────────────────────────────────────────


@_skip_node_unavailable
class TestSessionCreated:
    """A protected file asks once on creation, then passes for the rest of that session."""

    def test_creation_asks_then_edits_pass(self, run_guard: Callable[..., dict], tmp_path: Path) -> None:
        """The first write to a missing file asks; once it exists, later writes this session pass.

        Re-asking on every edit of a file the user just approved creating only repeated the same answer.
        """
        first = run_guard("AGENTS.md", tool_name="Write")
        (tmp_path / "AGENTS.md").write_text("x\n", encoding="utf-8")
        later = run_guard("AGENTS.md")
        assert (_asks(first), later) == (True, {})

    def test_denied_creation_retry_still_asks(self, run_guard: Callable[..., dict]) -> None:
        """A creation the user denied leaves no file, so the retry asks again.

        The ledger alone must not open the gate: the model retrying a denied ``Write`` is the case it would hide.
        """
        run_guard("AGENTS.md", tool_name="Write")
        assert _asks(run_guard("AGENTS.md", tool_name="Write"))

    def test_preexisting_untracked_file_keeps_asking(self, run_guard: Callable[..., dict], tmp_path: Path) -> None:
        """A protected file that existed before the session asked about it is not session-created.

        Nothing records who made it, so every write still needs a confirmation.
        """
        (tmp_path / "AGENTS.md").write_text("x\n", encoding="utf-8")
        run_guard("AGENTS.md")
        assert _asks(run_guard("AGENTS.md"))

    def test_other_session_does_not_inherit(self, run_guard: Callable[..., dict], tmp_path: Path) -> None:
        """A second session editing the same new file asks, because it did not create it.

        Ledgers are keyed by session id so concurrent sessions never vouch for each other's files.
        """
        run_guard("AGENTS.md", tool_name="Write", session_id="session-a")
        (tmp_path / "AGENTS.md").write_text("x\n", encoding="utf-8")
        assert _asks(run_guard("AGENTS.md", session_id="session-b"))

    def test_dangling_symlink_is_not_a_creation(self, run_guard: Callable[..., dict], tmp_path: Path) -> None:
        """A symlink to a missing target counts as existing, so it is never recorded or passed.

        Treating it as a creation would let a write through the link reach a protected file elsewhere unasked.
        """
        (tmp_path / "AGENTS.md").symlink_to(tmp_path / "missing-target.md")
        run_guard("AGENTS.md", tool_name="Write")
        assert _asks(run_guard("AGENTS.md"))


# ── The decision itself ───────────────────────────────────────────────────────


@_skip_node_unavailable
class TestDecisionShape:
    """The emitted envelope grants nothing and rewrites nothing."""

    def test_never_emits_allow(self, run_guard: Callable[..., dict]) -> None:
        """The hook has no allow path — an Edit carries no provenance to justify one.

        This is the design inversion against ``blueprint-allow.js``, which may allow because its input is verbatim text
        from a reviewed versioned file. Guarding the constant here keeps a later "just allow src/**" convenience patch
        from silently turning this into a grant.
        """
        source = HOOK.read_text(encoding="utf-8")
        decisions = re.findall(r'permissionDecision:\s*"(\w+)"', source)
        assert decisions == ["ask"]
        assert _asks(run_guard("CLAUDE.md"))

    def test_emits_no_updated_input(self, run_guard: Callable[..., dict]) -> None:
        """The ask must not rewrite the tool input — the user confirms the real write."""
        result = run_guard("CLAUDE.md")
        assert "updatedInput" not in result["hookSpecificOutput"]

    def test_hook_event_name(self, run_guard: Callable[..., dict]) -> None:
        """The envelope names its event, which is how the harness routes the decision."""
        result = run_guard("CLAUDE.md")
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
