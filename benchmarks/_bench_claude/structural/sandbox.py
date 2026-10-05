"""Disposable worktree staging and patch application for executable tasks."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from _bench_common.mutation_isolation import (
    IsolatedMutationCell,
    MutationCleanupError,
)

from _bench_claude.structural.config import (
    _PYTEST_RESULT_EXIT_CODES,
    PYTEST_EXIT_ALL_PASSED,
    SandboxError,
    _describe_pytest_exit,
    _pin_pytest_interpreter,
)

# ---------------------------------------------------------------------------
# Diff-impact staging
# ---------------------------------------------------------------------------


class DirtyTreeError(Exception):
    """Raised when the target tree already has uncommitted changes at DI-series start.

    A diff-impact task stages a synthetic change and reverts it with ``git checkout -- <paths>``. That revert is only
    safe when the touched paths were clean beforehand — reverting a path the user had already modified would silently
    destroy their edits. So the series refuses to run against a dirty tree rather than risk clobbering pre-existing
    changes.
    """


class DiffImpactStager:
    """Stage a scripted synthetic change in the target repository, then robustly revert it.

    A diff-impact task ships a ``stage`` spec containing either a file/find/replace mapping or a file/append mapping for
    each edit to a widely called signature or function body. The stager applies every edit inside a ``with`` block. On
    exit, whether successful or exceptional, it reverts every touched path with ``git checkout -- <path>`` so the change
    is present for both arms of the task and gone afterwards. The tree is verified clean (via
    ``git status --porcelain`` scoped to the touched paths) before staging: a dirty path aborts the
    whole series with :class:`DirtyTreeError` rather than risk clobbering the user's own edits.

    Args:
        repo_path: Root of the target repository (a git clone).
        stage_spec: List of edit dicts from the task's ``stage`` field.
    """

    def __init__(self, repo_path: str | Path, stage_spec: list[dict]) -> None:
        self.repo_path = Path(repo_path)
        self.stage_spec = stage_spec
        self._touched: list[Path] = []
        self.revert_error: str | None = None

    def _rel_paths(self) -> list[str]:
        """Return the repo-relative paths named by the stage spec (deduplicated, order-preserving)."""
        seen: dict[str, None] = {}
        for edit in self.stage_spec:
            rel = edit.get("file", "")
            if rel:
                seen.setdefault(rel, None)
        return list(seen)

    def _assert_clean(self) -> None:
        """Raise :class:`DirtyTreeError` when any staged path already has uncommitted changes."""
        rels = self._rel_paths()
        if not rels:
            return
        proc = subprocess.run(
            ["git", "-C", str(self.repo_path), "status", "--porcelain", "--", *rels],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise DirtyTreeError(f"git status failed in {self.repo_path}: {proc.stderr.strip()}")
        if proc.stdout.strip():
            raise DirtyTreeError(
                f"target tree is dirty at DI start — refusing to stage (would risk clobbering): "
                f"{proc.stdout.strip().splitlines()[:5]}"
            )

    def _apply(self) -> None:
        """Apply every edit in the stage spec, recording each touched path for revert."""
        for edit in self.stage_spec:
            rel = edit.get("file", "")
            if not rel:
                continue
            fpath = self.repo_path / rel
            text = fpath.read_text(encoding="utf-8")
            if "append" in edit:
                text = text + edit["append"]
            elif "find" in edit and "replace" in edit:
                if edit["find"] not in text:
                    raise DirtyTreeError(f"stage find-text not present in {rel}: {edit['find']!r}")
                text = text.replace(edit["find"], edit["replace"], 1)
            else:
                raise DirtyTreeError(f"stage edit for {rel} needs 'append' or 'find'+'replace'")
            fpath.write_text(text, encoding="utf-8")
            if fpath not in self._touched:
                self._touched.append(fpath)

    def revert(self) -> None:
        """Restore every touched path via ``git checkout -- <path>``.

        Never raises, so it is safe from ``__enter__``'s failure path and from
        ``__exit__`` while another exception propagates. The git result is no longer
        discarded: a failed revert leaves the *shared* target tree carrying the staged
        synthetic change, which every later task then sees as a dirty tree far from the
        task that caused it. The failure is recorded in ``revert_error`` and the touched
        paths are retained, so ``__exit__`` can escalate and the evidence survives.
        """
        rels = self._rel_paths()
        if not rels:
            return
        try:
            restored = subprocess.run(
                ["git", "-C", str(self.repo_path), "checkout", "--", *rels],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            self.revert_error = f"git checkout raised: {exc}"
            return
        if restored.returncode != 0:
            self.revert_error = (
                f"git checkout exited {restored.returncode} for {', '.join(rels)}: {restored.stderr.strip()[:300]}"
            )
            return
        self.revert_error = None
        self._touched = []

    def __enter__(self) -> DiffImpactStager:
        self._assert_clean()
        try:
            self._apply()
        except Exception:
            # A later edit's anchor may be missing (stale spec vs repo drift) after earlier edits
            # already wrote to disk. __exit__ does NOT run when __enter__ raises, so revert here or
            # the target tree is left partially modified and every subsequent DI task sees a dirty tree.
            self.revert()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # Revert runs whether the arms succeeded or raised — the change must never outlive the task.
        self.revert()
        if self.revert_error is None:
            return
        # The shared tree is still mutated. Escalate so the run stops here rather than
        # letting every later task inherit the contamination, but never mask an
        # in-flight exception that is already carrying the real cause.
        if exc_type is None:
            raise DirtyTreeError(
                f"staged diff-impact change was not reverted; target tree is still mutated: {self.revert_error}"
            )


# ---------------------------------------------------------------------------
# Patch sandbox (Tier E)
# ---------------------------------------------------------------------------

# Match the start of a unified diff: a `--- ` / `+++ ` header pair followed by an
# `@@` hunk header. Anchored at line start (MULTILINE) so prose preceding the diff
# is skipped; the diff is assumed to run to EOF (agents emit one diff block last).
_DIFF_RE = re.compile(r"^(?:--- .+\n\+\+\+ .+\n@@.+)", re.MULTILINE)


def _extract_diff(text: str) -> str | None:
    """Return the first unified diff block found in *text*, or None.

    The match anchors on a ``---``/``+++``/``@@`` header sequence and returns
    everything from there to the end of the string, since agents emit the patch
    as a trailing fenced block. Surrounding markdown fences (```` ```diff ````)
    are stripped from the tail when present.

    Args:
        text: Full agent response text.

    Returns:
        The unified diff substring, or None when no diff header is found.

    Examples:
        >>> _extract_diff("here is the fix\\n--- a/x.py\\n+++ b/x.py\\n@@ -1 +1 @@\\n-a\\n+b\\n")
        '--- a/x.py\\n+++ b/x.py\\n@@ -1 +1 @@\\n-a\\n+b\\n'
        >>> _extract_diff("no diff here") is None
        True
    """
    match = _DIFF_RE.search(text)
    if not match:
        return None
    diff = text[match.start() :]
    # Drop a trailing markdown code fence if the agent wrapped the diff.
    fence = diff.find("\n```")
    if fence != -1:
        diff = diff[:fence]
    if not diff.endswith("\n"):
        diff += "\n"
    return diff


class PatchSandbox:
    """Apply an agent-produced diff in an isolated git worktree and run its test.

    The sandbox checks out the task's pre-fix commit in a detached ``git worktree``
    under ``/tmp``, applies the candidate diff, runs the single failing test, then
    tears the worktree down. It measures one signal only: whether that specific
    test passes after the patch — not full-suite health or semantic correctness.

    Args:
        repo_path: Root of a local clone of the target repo with full git history.
        task: Patch-task dict; must carry ``id``, ``pre_fix_commit``, and either
            ``test_command`` or ``failing_test``.
    """

    def __init__(self, repo_path: str | Path, task: dict) -> None:
        self.repo_path = Path(repo_path)
        self.task = task
        self._worktree: Path | None = None
        self._worktree_active = False
        self.last_mutation_evidence: dict[str, Any] = {}

    def _test_argv(self) -> list[str]:
        """Build the pytest argv from the task's test_command or failing_test.

        A bare ``pytest`` resolves against ``PATH`` and can select an interpreter other than the one running the harness
        — three benchmark lanes were resolving pytest three different ways while their results were compared as one
        measurement. The binary is pinned to ``sys.executable -m pytest`` here, matching the codex lane.
        """
        cmd = self.task.get("test_command")
        if cmd:
            return _pin_pytest_interpreter(shlex.split(cmd))
        failing = self.task.get("failing_test")
        if not failing:
            raise SandboxError(f"task {self.task['id']}: no test_command or failing_test")
        return [sys.executable, "-m", "pytest", failing, "-x"]

    def run(self, diff_text: str) -> bool:
        """Apply *diff_text* at the pre-fix commit and run the failing test.

        Args:
            diff_text: Unified diff text produced by the agent.

        Returns:
            True when the test fails at the pre-fix commit, the patch applies
            cleanly, and the test passes after patching.
            False when the patch fails to apply, the test still fails after
            patching, or the test already passes before patching.

        Raises:
            SandboxError: When the worktree cannot be created (e.g. unknown
                pre-fix commit) — the pass/fail signal is then unobtainable.
        """
        commit = self.task.get("pre_fix_commit")
        if not commit:
            raise SandboxError(f"task {self.task['id']}: missing pre_fix_commit")

        cell = IsolatedMutationCell(self._allocate_worktree, self._cleanup)

        def evaluate(worktree: Path) -> bool:
            create = subprocess.run(
                ["git", "-C", str(self.repo_path), "worktree", "add", "--detach", str(worktree), commit],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if create.returncode != 0:
                raise SandboxError(f"task {self.task['id']}: worktree add failed at {commit}: {create.stderr.strip()}")
            self._worktree_active = True

            # Verify the test fails at the pre-fix commit before applying the patch.
            baseline = subprocess.run(
                [*self._test_argv(), "--timeout=60", "-q"],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=600,
            )
            if baseline.returncode not in _PYTEST_RESULT_EXIT_CODES:
                # pytest never produced a test result, so neither run is evidence about
                # the patch. Surfacing this as a sandbox error keeps the cell unscored
                # instead of recording a fabricated failure.
                raise SandboxError(
                    f"task {self.task['id']}: baseline pytest exited {baseline.returncode} "
                    f"({_describe_pytest_exit(baseline.returncode)}); "
                    f"no baseline test result: {baseline.stderr.strip()[:300]}"
                )
            if baseline.returncode == PYTEST_EXIT_ALL_PASSED:
                # Test already passes before the patch — cannot validate the fix.
                return False

            # Apply the diff. Prefer `git apply` (respects a/ b/ prefixes); fall back to patch -p1.
            # `--reject` is deliberately absent: it applies the hunks it can and still exits
            # non-zero, which left the fallback re-applying the same file onto an already
            # half-patched tree. The tree is reset between attempts for the same reason.
            patch_file = worktree / ".patch-bench.diff"
            patch_file.write_text(diff_text)
            applied = subprocess.run(
                ["git", "-C", str(worktree), "apply", "--whitespace=nowarn", str(patch_file)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if applied.returncode != 0:
                self._reset_worktree(worktree)
                fallback = subprocess.run(
                    ["patch", "-p1", "-i", str(patch_file)],
                    cwd=str(worktree),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if fallback.returncode != 0:
                    # Patch did not apply — count as a failed patch, not a sandbox error.
                    return False

            # Remove the harness's own scratch files so they cannot be collected as tests
            # or read as source by the scored run.
            self._clean_patch_artifacts(worktree)

            test = subprocess.run(
                [*self._test_argv(), "--timeout=60", "-q"],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=600,
            )
            if test.returncode not in _PYTEST_RESULT_EXIT_CODES:
                raise SandboxError(
                    f"task {self.task['id']}: post-patch pytest exited {test.returncode} "
                    f"({_describe_pytest_exit(test.returncode)}); "
                    f"no post-patch test result: {test.stderr.strip()[:300]}"
                )
            return test.returncode == PYTEST_EXIT_ALL_PASSED

        try:
            return cell.run(evaluate)
        except subprocess.TimeoutExpired:
            return False
        except MutationCleanupError as exc:
            raise SandboxError(f"task {self.task['id']}: {exc}") from exc
        finally:
            evidence = cell.last_evidence
            self.last_mutation_evidence = {
                "worktree": str(evidence.worktree) if evidence.worktree is not None else None,
                "action_error": evidence.action_error,
                "cleanup_error": evidence.cleanup_error,
                "restored": evidence.restored,
            }

    def _reset_worktree(self, worktree: Path) -> None:
        """Discard any partially applied hunks before the fallback apply attempt."""
        reset = subprocess.run(
            ["git", "-C", str(worktree), "checkout", "--", "."],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if reset.returncode != 0:
            raise SandboxError(
                f"task {self.task['id']}: worktree reset before fallback apply failed: {reset.stderr.strip()}"
            )

    def _clean_patch_artifacts(self, worktree: Path) -> None:
        """Remove the harness diff file and any ``.rej``/``.orig`` files it produced."""
        for path in [worktree / ".patch-bench.diff", *worktree.rglob("*.rej"), *worktree.rglob("*.orig")]:
            path.unlink(missing_ok=True)

    def _allocate_worktree(self) -> Path:
        """Allocate a unique private worktree path for one attempt or retry."""
        root = Path(tempfile.mkdtemp(prefix=f"patch-bench-{self.task['id']}-"))
        self._worktree = root / "repo"
        self._worktree_active = False
        return self._worktree

    def _cleanup(self, worktree: Path) -> None:
        """Restore and remove one private worktree or raise with cleanup evidence."""
        if self._worktree_active:
            reset = subprocess.run(
                ["git", "-C", str(worktree), "reset", "--hard", "HEAD"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if reset.returncode != 0:
                raise SandboxError(f"task {self.task['id']}: worktree reset failed: {reset.stderr.strip()}")
            remove = subprocess.run(
                ["git", "-C", str(self.repo_path), "worktree", "remove", str(worktree)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if remove.returncode != 0:
                raise SandboxError(f"task {self.task['id']}: worktree remove failed: {remove.stderr.strip()}")
            self._worktree_active = False
        if worktree.exists():
            raise SandboxError(f"task {self.task['id']}: worktree remains after cleanup")
        try:
            worktree.parent.rmdir()
        except OSError as exc:
            raise SandboxError(f"task {self.task['id']}: worktree parent cleanup failed") from exc
