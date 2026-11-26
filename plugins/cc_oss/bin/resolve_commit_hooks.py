#!/usr/bin/env python
"""Commit resolved changes with an explicit repository-hook approval boundary.

Purpose:
    Keep contributor-supplied hooks disabled unless the user approves their displayed contents for this run. Scope: The
    resolve commit helpers share this boundary; it does not sandbox approved hooks or Git filters. Usage: Run ``python
    resolve_commit_hooks.py`` to display JSON containing the hook commands, configuration and SHA256.     After user
    approval only, pass that digest in ``RESOLVE_APPROVED_HOOKS`` to the commit helpers.     ``--commit-merge`` finishes
    an active merge with the same hook boundary and bounded formatter recovery. Outputs: The inspector prints the active
    hooks, conventional hook configuration and their content fingerprint.     Commits without approval use a temporary
    empty hooks directory, without changing repository configuration. Failure: Changed approval contents refuse
    execution. A rejecting formatter may restage selected paths and retry once;     unrelated edits, untracked additions
    outside selection, unchanged files, and a second rejection stop recovery. Used by: commit_action_item.py,
    commit_all_items.py, commit_lint_fixes.py and the resolve dispatch modes.

Decision:
    ``--decide --base REF`` follows the local Git setup instead of always asking. No installed hook runs nothing, so
    there is nothing to ask. Installed hooks run when the PR leaves every in-tree hook input (hook scripts, ``.husky``,
    ``.githooks``, ``.pre-commit-config.*``, ``package.json``) identical to the base, because then the contributor
    supplies none of the hook code. A PR that changes a hook input, or a base that cannot be compared, yields ``ask``.
    ``--head REF`` applies only while a merge is pending (``MERGE_HEAD`` exists): the decision then compares what the
    PR head and the checked-out commit commit, plus unstaged and untracked edits and staged entries that match neither
    side of the merge, so base-side edits the merge already staged do not count. With no merge pending it is ignored
    and the working tree is judged as before.
    ``--remember approved --sha DIGEST`` saves the digest the user was shown (never one inspected later, since hooks can
    change between the question and the answer) and ``--remember never`` disables hooks for this repository in
    ``<git-common-dir>/resolve-hooks-policy.json``; ``--forget`` removes it. A saved approval covers the exact hook
    bytes only, so changed hooks ask again.

Refresh:
    ``--refresh-approval --approved-file FILE --decision-file FILE [--base REF] [--head REF]`` runs before every
    merge commit, because a merge can change hook inputs (a base-side ``package.json`` bump) after the approval was
    recorded, and a digest that no longer matches refuses the commit and leaves the merge pending. A missing approval
    file (a question still pending) and an empty one (hooks disabled) are left alone; a digest that still matches is
    kept. A stale digest the helper recorded itself (the decision file says ``run``) is decided again on the merged
    tree and replaced. Every other stale digest (a user's answer, a re-decision that now asks, a failed inspection)
    is set aside: the approval file is emptied and the decision file says ``stale``, so the merge and every later
    commit of the run go ahead with hooks disabled and never run hook bytes nobody approved. It prints one line,
    ``hooks-refresh=<outcome> — <reason>``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

_PLUMBING_TIMEOUT = 30
#: Seconds a ``--commit-merge`` commit may take, hooks included. With the ``MERGE_HEAD`` check before it, the call
#: ends within :data:`MERGE_COMMIT_WORST_CASE`, under the 600 s ceiling of the Bash call that runs it.
MERGE_COMMIT_BUDGET = 540
#: Upper bound of one ``--commit-merge`` call: the ``MERGE_HEAD`` check plus the commit budget.
MERGE_COMMIT_WORST_CASE = _PLUMBING_TIMEOUT + MERGE_COMMIT_BUDGET
_POLICY_FILE = "resolve-hooks-policy.json"
_POLICY_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
#: Repository-relative paths a contribution can change that decide which hook code runs.
_TREE_HOOK_INPUTS = (
    ".husky",
    ".githooks",
    ".huskyrc",
    ".huskyrc.json",
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
    "package.json",
)
_HOOK_DIRECTORY_LABELS = ("active-hooks/", ".husky/", ".githooks/")


class Decision(str, Enum):
    """Outcome of the repository-hook decision."""

    NONE = "none"
    RUN = "run"
    ASK = "ask"
    DISABLED = "disabled"


class PolicyMode(str, Enum):
    """Saved per-repository choice."""

    APPROVED = "approved"
    NEVER = "never"


class RefreshOutcome(str, Enum):
    """What ``--refresh-approval`` did to the recorded hook approval before a merge commit."""

    PENDING = "pending"
    DISABLED = "disabled"
    CURRENT = "current"
    REFRESHED = "refreshed"
    STALE = "stale"


#: Decision-file word for an approval set aside because the hooks it covered changed.
STALE_DECISION = "stale"


@dataclass
class HookDecision:
    """Decision, its reason, the inspected digest and the hook inputs the PR changed."""

    decision: Decision
    reason: str
    sha256: str = ""
    changed: list[str] = field(default_factory=list)
    inspection: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CallBudget:
    """A wall-clock budget shared by every process one commit starts.

    The Bash call that runs a commit helper has a fixed timeout. A per-process timeout alone cannot stay under it: an
    approved-hook commit runs up to two commit attempts plus the plumbing between them, so its worst case is several
    timeouts long, and the Bash call can be killed between two steps. Every process started under one budget gets
    whichever is smaller, its own cap or the time the budget has left, so the whole call ends within the budget.

    Attributes:
        expires: ``time.monotonic()`` value at which the budget is spent.
    """

    expires: float

    @classmethod
    def starting_now(cls, seconds: float) -> CallBudget:
        """Return a budget of ``seconds`` counted from now."""
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        """Return the seconds left, or raise :class:`subprocess.TimeoutExpired` when the budget is spent."""
        left = self.expires - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired("git", 0)
        return left

    def cap(self, limit: float) -> float:
        """Return the timeout for the next process: ``limit`` or the time left, whichever is smaller.

        Examples:
            >>> CallBudget.starting_now(600).cap(30)
            30
            >>> CallBudget.starting_now(5).cap(30) <= 5
            True
        """
        return min(limit, self.remaining())


def _timeout(budget: CallBudget | None, limit: float = _PLUMBING_TIMEOUT) -> float:
    """Return ``limit`` alone, or capped by ``budget`` when the call runs under one."""
    return limit if budget is None else budget.cap(limit)


def _merge_in_progress(git: str) -> bool:
    """Tell whether a merge is pending (``MERGE_HEAD`` exists), i.e. the index may hold base-side staged content."""
    done = subprocess.run(  # noqa: S603
        [git, "rev-parse", "-q", "--verify", "MERGE_HEAD"], capture_output=True, timeout=_PLUMBING_TIMEOUT
    )
    return done.returncode == 0


def _git_output(git: str, *args: str, budget: CallBudget | None = None) -> bytes:
    """Read bounded Git plumbing output without executing external diff commands."""
    return subprocess.run(  # noqa: S603
        [git, *args], check=True, capture_output=True, timeout=_timeout(budget)
    ).stdout


def _file_record(path: Path, label: str) -> dict[str, str | int]:
    """Describe exact file bytes, link target and mode without losing undecodable content."""
    content = path.read_bytes()
    return {
        "path": label,
        "mode": path.stat().st_mode,
        "link": os.readlink(path) if path.is_symlink() else "",
        "content": content.decode("utf-8", errors="backslashreplace"),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def inspect_repository_hooks(git: str, budget: CallBudget | None = None) -> dict[str, object]:
    """Display and fingerprint active hooks and conventional repository hook configuration.

    The fingerprint includes contents, executable modes and configured hook location. It is portable between resolve
    worktrees whose relative hook paths and contents agree; an absolute hook location remains absolute in the report.
    The displayed hook scripts define executable commands, including any dynamic dispatch they perform. ``budget`` caps
    each git call when the inspection runs inside a commit (see :class:`CallBudget`).
    """
    hooks_path = os.fsdecode(_git_output(git, "rev-parse", "--git-path", "hooks", budget=budget)).strip()
    config = subprocess.run(  # noqa: S603
        [git, "config", "--show-origin", "--get-all", "core.hooksPath"],
        check=False,
        capture_output=True,
        timeout=_timeout(budget),
    )
    if config.returncode not in (0, 1):
        raise RuntimeError("cannot inspect core.hooksPath configuration")
    root = Path(os.fsdecode(_git_output(git, "rev-parse", "--show-toplevel", budget=budget)).strip())
    hook_directory = Path(hooks_path)
    files: dict[str, dict[str, str | int]] = {}
    locations = [(hook_directory, "active-hooks"), (root / ".husky", ".husky"), (root / ".githooks", ".githooks")]
    for directory, label in locations:
        if directory.is_dir():
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    key = f"{label}/{path.relative_to(directory).as_posix()}"
                    files[key] = _file_record(path, key)
    for name in (".pre-commit-config.yaml", ".pre-commit-config.yml", "package.json", ".huskyrc", ".huskyrc.json"):
        path = root / name
        if path.is_file():
            files[name] = _file_record(path, name)
    # Default .git paths differ between linked worktrees; their contents carry the approval identity instead.
    report: dict[str, object] = {
        "core.hooksPath": config.stdout.decode(errors="backslashreplace"),
        "files": list(files.values()),
    }
    report["sha256"] = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    report["active_hooks_directory"] = hooks_path
    return report


def _policy_path(git: str) -> Path:
    """Locate the policy file in the Git common directory, which linked worktrees of one repository share."""
    common = os.fsdecode(_git_output(git, "rev-parse", "--git-common-dir")).strip()
    return Path(common).resolve() / _POLICY_FILE


def _load_policy(git: str) -> tuple[PolicyMode, str] | None:
    """Read the saved policy; a missing, unreadable or unrecognised file grants nothing."""
    try:
        data = json.loads(_policy_path(git).read_text(encoding="utf-8"))
        if data["version"] != _POLICY_VERSION:
            return None
        return PolicyMode(data["mode"]), str(data.get("sha256", ""))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_policy(git: str, mode: PolicyMode, approved_sha: str = "") -> Path:
    """Save the per-repository choice; an approval binds to ``approved_sha``, the digest the user was shown.

    Raises:
        ValueError: If an approval carries anything but a 64-digit lowercase hex digest.
    """
    sha = approved_sha if mode is PolicyMode.APPROVED else ""
    if mode is PolicyMode.APPROVED and not _SHA256.fullmatch(sha):
        raise ValueError("an approved policy needs the shown 64-digit sha256")
    path = _policy_path(git)
    record = {"version": _POLICY_VERSION, "mode": mode.value, "sha256": sha}
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=".resolve-hooks-", delete=False
    ) as tmp:
        json.dump(record, tmp)
    os.replace(tmp.name, path)
    return path


def _has_active_hooks(report: dict[str, Any]) -> bool:
    """Tell whether any executable hook exists; Git's ``.sample`` files never run."""
    return any(
        record["path"].startswith(_HOOK_DIRECTORY_LABELS) and not record["path"].endswith(".sample")
        for record in report["files"]
    )


def _tree_hook_inputs(git: str, hooks_path: str) -> list[str]:
    """List the in-tree pathspecs a contribution could use to supply hook code."""
    specs = list(_TREE_HOOK_INPUTS)
    root = Path(os.fsdecode(_git_output(git, "rev-parse", "--show-toplevel")).strip()).resolve()
    git_dir = Path(os.fsdecode(_git_output(git, "rev-parse", "--git-common-dir")).strip()).resolve()
    hooks = Path(hooks_path).resolve()
    if hooks.is_relative_to(root) and not hooks.is_relative_to(git_dir):
        specs.append(hooks.relative_to(root).as_posix())
    return specs


def _changed_inputs(git: str, base: str, specs: list[str], head: str | None = None) -> list[str]:
    """Return hook inputs that differ from the merge base.

    Without ``head`` the comparison runs against the working tree (committed, staged, unstaged or untracked). With
    ``head`` and a merge in progress (``MERGE_HEAD`` exists) it counts what that commit changes since the merge base,
    what the checked-out commit changes since its own merge base with ``base`` (PR commits pulled after ``head`` was
    recorded, this run's own commits), unstaged and untracked edits, and every staged entry (content, mode, deletion or
    unresolved conflict) that matches neither the checked-out commit nor ``MERGE_HEAD``: such an entry was written
    during the merge, by conflict resolution for example, so it is neither the PR's commits nor the base. What the merge
    staged from either side does not count. Without a merge in progress ``head`` is ignored, so a clean pre-merge
    checkout is judged exactly as before and a staged edit still counts.
    """
    for ref in (base, head):
        if ref is not None and ref.startswith("-"):
            raise RuntimeError(f"{ref!r} is not a reference")
    if head and not _merge_in_progress(git):
        head = None
    merge_base = os.fsdecode(_git_output(git, "merge-base", base, head or "HEAD")).strip()
    diff = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", merge_base)
    tracked = _git_output(git, "--literal-pathspecs", *diff, *([head] if head else []), "--", *specs)
    if head:
        # the checked-out commit can hold more than ``head``: PR commits pulled after it was recorded, and this
        # run's own commits after an earlier merge; what it commits beyond the base counts as well
        checkout_base = os.fsdecode(_git_output(git, "merge-base", base, "HEAD")).strip()
        committed = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", checkout_base)
        tracked += b"\0" + _git_output(git, "--literal-pathspecs", *committed, "HEAD", "--", *specs)
    untracked = _git_output(
        git, "--literal-pathspecs", "ls-files", "--others", "--exclude-standard", "-z", "--", *specs
    )
    names = {os.fsdecode(name) for name in (tracked + untracked).split(b"\0") if name}
    if head:
        # worktree-vs-index edits only: what the staged merge brought in is in the index, so it stays out
        unstaged_diff = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", "--")
        names |= {os.fsdecode(n) for n in _git_output(git, "--literal-pathspecs", *unstaged_diff, *specs).split(b"\0")}
        names |= _staged_from_neither_side(git, specs)
    return sorted(name for name in names if name)


def _staged_from_neither_side(git: str, specs: list[str]) -> set[str]:
    """Return staged hook inputs whose index entry differs from both ``HEAD`` and ``MERGE_HEAD``.

    ``git diff --cached <commit>`` lists every path whose index entry (blob, mode, presence or unmerged state) differs
    from that commit, so a path in both lists matches neither side of the merge.
    """
    sides = []
    for commit in ("HEAD", "MERGE_HEAD"):
        cached = ("diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", commit)
        raw = _git_output(git, "--literal-pathspecs", *cached, "--", *specs)
        sides.append({os.fsdecode(name) for name in raw.split(b"\0") if name})
    return sides[0] & sides[1]


def _classify(git: str, base: str, report: dict[str, Any], head: str | None = None) -> HookDecision:
    """Apply the saved policy, then the installed-hooks and PR-change rules, to one hook inspection."""
    sha = str(report["sha256"])
    policy = _load_policy(git)
    if policy and policy[0] is PolicyMode.NEVER:
        return HookDecision(Decision.DISABLED, "saved policy: never run hooks in this repository", sha)
    if not _has_active_hooks(report):
        return HookDecision(Decision.NONE, "no hook is installed, so nothing would run", sha)
    if policy and policy[1] == sha:
        return HookDecision(Decision.RUN, "saved approval matches these exact hooks", sha)
    try:
        changed = _changed_inputs(git, base, _tree_hook_inputs(git, str(report["active_hooks_directory"])), head)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return HookDecision(Decision.ASK, f"cannot compare hook inputs with base {base}: {exc}", sha)
    if changed:
        return HookDecision(Decision.ASK, "the PR changes hook inputs", sha, changed)
    return HookDecision(Decision.RUN, "installed hooks and the PR leaves every hook input identical to base", sha)


def decide_hooks(git: str, base: str, head: str | None = None) -> HookDecision:
    """Decide whether repository hooks run, are disabled, or need the user's approval."""
    report = inspect_repository_hooks(git)
    decision = _classify(git, base, report, head)
    decision.inspection = report
    return decision


def _first_line(path: Path) -> str:
    """Return the first line of ``path`` without surrounding whitespace, or an empty string when it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    return text.splitlines()[0].strip() if text.strip() else ""


def _set_aside(approved_file: Path, decision_file: Path, reason: str) -> tuple[RefreshOutcome, str]:
    """Disable hooks for the rest of the run: empty the approval and record ``stale`` as the decision."""
    approved_file.write_text("", encoding="utf-8")
    decision_file.write_text(STALE_DECISION + "\n", encoding="utf-8")
    return RefreshOutcome.STALE, f"{reason}; hooks stay disabled for the rest of this run"


def refresh_approval(
    git: str, approved_file: Path, decision_file: Path, base: str | None = None, head: str | None = None
) -> tuple[RefreshOutcome, str]:
    """Keep the recorded hook approval valid for the tree about to be committed, or set it aside.

    Only a digest this helper recorded itself (``decision_file`` says ``run``) is decided again, on the current tree
    with ``base`` and ``head``. A user's approval is never re-decided: when the hooks it covered changed it is set
    aside like every other approval that cannot be refreshed, so the commit goes ahead with hooks disabled.

    Raises:
        OSError: If the approval file exists but cannot be read, or the files cannot be written.
    """
    try:
        recorded = approved_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return RefreshOutcome.PENDING, "no hook approval recorded yet, so this commit runs with hooks disabled"
    if not recorded:
        return RefreshOutcome.DISABLED, "hooks are disabled for this run"
    try:
        if str(inspect_repository_hooks(git)["sha256"]) == recorded:
            return RefreshOutcome.CURRENT, "the recorded approval matches these exact hooks"
        decision = _decide_again(git, decision_file, base, head)
    except _CannotRefreshError as exc:
        return _set_aside(approved_file, decision_file, str(exc))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return _set_aside(approved_file, decision_file, f"hook inspection failed: {exc}")
    if decision.decision is Decision.ASK:
        detail = ", ".join(decision.changed) or decision.reason
        return _set_aside(approved_file, decision_file, f"the merged tree changes hook inputs: {detail}")
    approved_file.write_text(decision.sha256 if decision.decision is Decision.RUN else "", encoding="utf-8")
    decision_file.write_text(decision.decision.value + "\n", encoding="utf-8")
    return RefreshOutcome.REFRESHED, f"{decision.decision.value}: {decision.reason}"


class _CannotRefreshError(Exception):
    """A stale approval that only a person could renew: a user's answer, or a helper decision without a base."""


def _decide_again(git: str, decision_file: Path, base: str | None, head: str | None) -> HookDecision:
    """Decide a stale helper-made approval again on the current tree.

    Raises:
        _CannotRefreshError: If the approval was not made by this helper or there is no base to compare with.
    """
    if _first_line(decision_file) != Decision.RUN.value:
        raise _CannotRefreshError("the hooks changed since they were approved")
    if not base:
        raise _CannotRefreshError("no base reference to decide the hooks again")
    return decide_hooks(git, base, head or None)


def _working_files(git: str, budget: CallBudget | None = None) -> dict[str, tuple[int, str] | None]:
    """Snapshot dirty tracked and nonignored untracked files, including deletions and symlinks."""
    diff = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", "HEAD")
    raw = _git_output(git, *diff, budget=budget)
    raw += _git_output(git, "ls-files", "--others", "--exclude-standard", "-z", budget=budget)
    state: dict[str, tuple[int, str] | None] = {}
    for name in raw.split(b"\0"):
        if not name:
            continue
        path = Path(os.fsdecode(name))
        key = path.as_posix()
        if path.is_symlink():
            state[key] = (path.lstat().st_mode, os.readlink(path))
        elif path.is_file():
            state[key] = (path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            state[key] = None
    return state


def commit_with_hooks(git: str, args: list[str], files: list[str] | None, timeout: float) -> int:
    """Run a commit with hooks disabled, or approved hooks with one scoped formatter recovery.

    Args:
        git: Git executable.
        args: Git arguments of the commit.
        files: Files the commit selects (the paths a formatter hook may rewrite); ``None`` reads the staged list.
        timeout: Seconds for the whole call: both commit attempts and every git call between them share it, so the
            call ends within ``timeout`` whatever the hooks do (see :class:`CallBudget`).

    Returns:
        The commit's exit code; ``1`` when the approval no longer matches or a hook stepped outside the selection.

    Raises:
        subprocess.TimeoutExpired: When the budget is spent, including before a retry could start.
    """
    if timeout <= 0:
        raise subprocess.TimeoutExpired("git", 0)
    approval = os.environ.get("RESOLVE_APPROVED_HOOKS", "")
    if not approval:
        # one process: it gets the whole budget
        with tempfile.TemporaryDirectory(prefix="resolve-no-hooks-") as empty:
            return subprocess.run(  # noqa: S603
                [git, "-c", f"core.hooksPath={empty}", *args], check=False, timeout=timeout
            ).returncode
    try:
        return _commit_approved(git, args, files, CallBudget.starting_now(timeout), approval)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"resolve hooks: {exc} — no further commit attempted", file=sys.stderr)
        return 1


def _index_entries(git: str, budget: CallBudget | None = None) -> dict[str, bytes]:
    """Capture staged object identities so hooks cannot widen a recovery commit through index-only changes."""
    return {
        os.fsdecode(entry.split(b"\t", 1)[1]): entry.split(b"\t", 1)[0]
        for entry in _git_output(git, "ls-files", "--stage", "-z", budget=budget).split(b"\0")
        if entry
    }


def _commit_approved(git: str, args: list[str], files: list[str] | None, budget: CallBudget, approval: str) -> int:
    """Validate the displayed approval and recover only a formatter's selected-file edits, all within ``budget``."""
    if inspect_repository_hooks(git, budget)["sha256"] != approval:
        raise RuntimeError("hook commands/configuration changed or approval is invalid; show them and ask again")
    selected = files
    if selected is None:
        raw = _git_output(git, "diff", "--cached", "--name-only", "--no-renames", "-z", budget=budget)
        selected = [os.fsdecode(name) for name in raw.split(b"\0") if name]
    before = _working_files(git, budget)
    staged_before = _index_entries(git, budget)
    result = subprocess.run([git, *args], check=False, timeout=budget.remaining())  # noqa: S603
    if result.returncode == 0:
        return 0
    after = _working_files(git, budget)
    staged_after = _index_entries(git, budget)
    index_changes = {
        name for name in staged_before.keys() | staged_after.keys() if staged_before.get(name) != staged_after.get(name)
    }
    if index_changes - {Path(name).as_posix() for name in selected}:
        raise RuntimeError("hook staged paths outside the selected files; inspect the index before continuing")
    clean = object()
    changed = {name for name in before.keys() | after.keys() if before.get(name, clean) != after.get(name, clean)}
    if not changed:
        return result.returncode
    if changed - {Path(name).as_posix() for name in selected}:
        raise RuntimeError("hook changed paths outside the selected files; inspect the tree before continuing")
    if inspect_repository_hooks(git, budget)["sha256"] != approval:
        raise RuntimeError("hook commands/configuration changed during execution; show them and ask again")
    subprocess.run(  # noqa: S603
        [git, "--literal-pathspecs", "add", "--", *sorted(changed)], check=True, timeout=_timeout(budget)
    )
    print("resolve hooks: restaged hook-modified selected files; retrying commit once", file=sys.stderr)
    retry = subprocess.run([git, *args], check=False, timeout=budget.remaining())  # noqa: S603
    if retry.returncode:
        print("resolve hooks: second commit attempt failed; inspect hook output and working tree", file=sys.stderr)
    return retry.returncode


def _print_decision(decision: HookDecision, approved_file: str | None) -> None:
    """Print the decision as JSON; unless it needs a question, record the approval digest (empty when none applies)."""
    if approved_file and decision.decision is not Decision.ASK:
        digest = decision.sha256 if decision.decision is Decision.RUN else ""
        Path(approved_file).write_text(digest, encoding="utf-8")
    print(json.dumps({**asdict(decision), "decision": decision.decision.value}, ensure_ascii=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    """Inspect hooks for parent approval, decide from the local Git setup, or finish an active merge."""
    parser = argparse.ArgumentParser(description="Display repository hook commands/configuration for per-run approval.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--commit-merge", action="store_true", help="Finish an active merge using the hook boundary.")
    action.add_argument("--decide", action="store_true", help="Decide none/run/ask/disabled from Git and the PR diff.")
    action.add_argument(
        "--refresh-approval",
        action="store_true",
        help="Before a merge commit: keep, re-decide (helper-made run) or set aside (stale) the recorded approval.",
    )
    action.add_argument("--remember", choices=[mode.value for mode in PolicyMode], help="Save a per-repository choice.")
    action.add_argument("--forget", action="store_true", help="Remove the saved per-repository choice.")
    parser.add_argument("--sha", help="With --remember approved: the hook digest the user was shown (required).")
    parser.add_argument("--base", help="Base reference the PR is compared with; required by --decide.")
    parser.add_argument(
        "--head",
        help="With --decide and a merge pending: compare what this PR head commits, plus unstaged and untracked"
        " edits, instead of the working tree; ignored when no merge is pending.",
    )
    parser.add_argument(
        "--approved-file",
        help="With --decide: write the digest (run) or an empty file (none, disabled). With --refresh-approval: the"
        " recorded approval to keep, replace or empty.",
    )
    parser.add_argument(
        "--decision-file",
        help="With --refresh-approval: the recorded decision word (run marks a helper-made approval).",
    )
    args = parser.parse_args(argv)
    if args.decide and not args.base:
        parser.error("--decide requires --base")
    if args.refresh_approval and not (args.approved_file and args.decision_file):
        parser.error("--refresh-approval requires --approved-file and --decision-file")
    if args.remember == PolicyMode.APPROVED.value and not _SHA256.fullmatch(args.sha or ""):
        parser.error("--remember approved requires --sha with the 64-digit digest the user was shown")
    try:
        if args.commit_merge:
            _git_output("git", "rev-parse", "--verify", "MERGE_HEAD")
            return commit_with_hooks("git", ["commit", "--no-edit"], None, MERGE_COMMIT_BUDGET)
        if args.decide:
            _print_decision(decide_hooks("git", args.base, args.head), args.approved_file)
        elif args.refresh_approval:
            outcome, reason = refresh_approval(
                "git", Path(args.approved_file), Path(args.decision_file), args.base, args.head
            )
            print(f"hooks-refresh={outcome.value} — {reason}")
        elif args.remember:
            print(
                f"resolve hooks: saved '{args.remember}' in {save_policy('git', PolicyMode(args.remember), args.sha or '')}"
            )
        elif args.forget:
            _policy_path("git").unlink(missing_ok=True)
        else:
            print(json.dumps(inspect_repository_hooks("git"), ensure_ascii=True, indent=2))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        stage = "merge commit" if args.commit_merge else "approval refresh" if args.refresh_approval else "inspection"
        print(f"resolve hooks: {stage} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
