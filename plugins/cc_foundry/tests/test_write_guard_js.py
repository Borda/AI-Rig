"""Subprocess tests for ``hooks/write-guard.js``.

The hook is a ``PreToolUse`` gate on ``Edit``/``Write``/``NotebookEdit`` that forces a
confirmation on writes to a small, repository-independent protected set (CI
definitions, agent instructions, lockfiles, release metadata, Claude's own
permission config). Its contract:

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

Every case drives the hook over stdin, which is the path the harness itself uses,
rather than calling ``decide()`` in process — the JSON envelope shape is part of
the contract and an in-process call would not exercise it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "write-guard.js"

NODE_UNAVAILABLE = shutil.which("node") is None


@pytest.fixture(name="run_guard")
def _run_guard() -> Callable[..., dict]:
    """Return a callable that feeds one payload to the hook and parses its stdout."""

    def _run(path: str, *, tool_name: str = "Edit", key: str = "file_path") -> dict:
        """Run the hook with one write-shaped payload and parse its result."""
        payload = json.dumps({"tool_name": tool_name, "tool_input": {key: path}})
        proc = subprocess.run(
            ["node", str(HOOK)],
            input=payload,
            capture_output=True,
            encoding="utf-8",
            timeout=10,
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
            pytest.param(".github/workflows/ci.yml", "CI/workflow definition", id="ci-workflow"),
            pytest.param("CLAUDE.md", "agent instructions", id="claude-md"),
            pytest.param("docs/nested/AGENTS.md", "agent instructions", id="nested-agents-md"),
            pytest.param(".claude/settings.local.json", "Claude permission config", id="permission-config"),
            pytest.param(".pre-commit-config.yaml", "lint/format gate", id="lint-gate"),
            pytest.param("CHANGELOG.md", "release metadata", id="changelog"),
            pytest.param("uv.lock", "dependency lockfile", id="lockfile"),
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

    def test_absolute_path(self, run_guard: Callable[..., dict]) -> None:
        """An absolute path matches the same as a repository-relative one.

        Tools may hand over either form depending on how the model addressed the file; a guard matching only relative
        paths would be trivially sidestepped by an absolute one.
        """
        assert _asks(run_guard("/abs/repo/.github/workflows/x.yml"))

    def test_simulated_windows_separators(self, run_guard: Callable[..., dict]) -> None:
        """A Windows-style path with backslashes still matches.

        Patterns are written for ``/`` and the input is normalized first. Without that step the entire guard would
        silently disappear on one OS while every macOS and Linux test stayed green — the project's recurrent defect
        class.
        """
        assert _asks(run_guard("C:\\repo\\.github\\workflows\\x.yml"))

    @pytest.mark.parametrize("path", ["changelog.md", "Claude.md", "UV.LOCK", ".GitHub/workflows/ci.yml"])
    def test_case_variants(self, run_guard: Callable[..., dict], path: str) -> None:
        """Case variants of a protected name still ask.

        macOS and Windows filesystems are case-folding, so a write addressed as ``changelog.md`` lands in the real
        ``CHANGELOG.md``. Under case-sensitive matching that path classifies as unprotected — and passthrough means
        auto-approved whenever the hook is paired with ``acceptEdits``, which is the configuration it exists for. This
        is the bypass, not a nicety.
        """
        assert _asks(run_guard(path)), f"{path!r} should ask — case-folding bypass"

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
        assert _asks(run_guard("CHANGELOG.md", tool_name=tool_name, key=key))


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
        assert _asks(run_guard("CHANGELOG.md"))

    def test_emits_no_updated_input(self, run_guard: Callable[..., dict]) -> None:
        """The ask must not rewrite the tool input — the user confirms the real write."""
        result = run_guard("CHANGELOG.md")
        assert "updatedInput" not in result["hookSpecificOutput"]

    def test_hook_event_name(self, run_guard: Callable[..., dict]) -> None:
        """The envelope names its event, which is how the harness routes the decision."""
        result = run_guard("CHANGELOG.md")
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
