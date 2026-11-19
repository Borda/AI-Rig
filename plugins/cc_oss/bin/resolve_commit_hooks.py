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
    ``--remember approved`` saves the inspected digest and ``--remember never`` disables hooks for this repository in
    ``<git-common-dir>/resolve-hooks-policy.json``; ``--forget`` removes it. A saved approval covers the exact hook
    bytes only, so changed hooks ask again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

_PLUMBING_TIMEOUT = 30
_POLICY_FILE = "resolve-hooks-policy.json"
_POLICY_VERSION = 1
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


@dataclass
class HookDecision:
    """Decision, its reason, the inspected digest and the hook inputs the PR changed."""

    decision: Decision
    reason: str
    sha256: str = ""
    changed: list[str] = field(default_factory=list)
    inspection: dict[str, Any] = field(default_factory=dict)


def _git_output(git: str, *args: str) -> bytes:
    """Read bounded Git plumbing output without executing external diff commands."""
    return subprocess.run(  # noqa: S603
        [git, *args], check=True, capture_output=True, timeout=_PLUMBING_TIMEOUT
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


def inspect_repository_hooks(git: str) -> dict[str, object]:
    """Display and fingerprint active hooks and conventional repository hook configuration.

    The fingerprint includes contents, executable modes and configured hook location. It is portable between resolve
    worktrees whose relative hook paths and contents agree; an absolute hook location remains absolute in the report.
    The displayed hook scripts define executable commands, including any dynamic dispatch they perform.
    """
    hooks_path = os.fsdecode(_git_output(git, "rev-parse", "--git-path", "hooks")).strip()
    config = subprocess.run(  # noqa: S603
        [git, "config", "--show-origin", "--get-all", "core.hooksPath"],
        check=False,
        capture_output=True,
        timeout=_PLUMBING_TIMEOUT,
    )
    if config.returncode not in (0, 1):
        raise RuntimeError("cannot inspect core.hooksPath configuration")
    root = Path(os.fsdecode(_git_output(git, "rev-parse", "--show-toplevel")).strip())
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


def save_policy(git: str, mode: PolicyMode) -> Path:
    """Save the per-repository choice, binding an approval to the digest of the hooks inspected now."""
    sha = str(inspect_repository_hooks(git)["sha256"]) if mode is PolicyMode.APPROVED else ""
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


def _changed_inputs(git: str, base: str, specs: list[str]) -> list[str]:
    """Return hook inputs that differ from the merge base: committed, staged, unstaged or untracked."""
    if base.startswith("-"):
        raise RuntimeError(f"base {base!r} is not a reference")
    merge_base = os.fsdecode(_git_output(git, "merge-base", base, "HEAD")).strip()
    diff = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", merge_base, "--")
    tracked = _git_output(git, "--literal-pathspecs", *diff, *specs)
    untracked = _git_output(
        git, "--literal-pathspecs", "ls-files", "--others", "--exclude-standard", "-z", "--", *specs
    )
    return sorted({os.fsdecode(name) for name in (tracked + untracked).split(b"\0") if name})


def _classify(git: str, base: str, report: dict[str, Any]) -> HookDecision:
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
        changed = _changed_inputs(git, base, _tree_hook_inputs(git, str(report["active_hooks_directory"])))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return HookDecision(Decision.ASK, f"cannot compare hook inputs with base {base}: {exc}", sha)
    if changed:
        return HookDecision(Decision.ASK, "the PR changes hook inputs", sha, changed)
    return HookDecision(Decision.RUN, "installed hooks and the PR leaves every hook input identical to base", sha)


def decide_hooks(git: str, base: str) -> HookDecision:
    """Decide whether repository hooks run, are disabled, or need the user's approval."""
    report = inspect_repository_hooks(git)
    decision = _classify(git, base, report)
    decision.inspection = report
    return decision


def _working_files(git: str) -> dict[str, tuple[int, str] | None]:
    """Snapshot dirty tracked and nonignored untracked files, including deletions and symlinks."""
    raw = _git_output(git, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", "HEAD")
    raw += _git_output(git, "ls-files", "--others", "--exclude-standard", "-z")
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


def commit_with_hooks(git: str, args: list[str], files: list[str] | None, timeout: int) -> int:
    """Run a commit with hooks disabled, or approved hooks with one scoped formatter recovery."""
    approval = os.environ.get("RESOLVE_APPROVED_HOOKS", "")
    if not approval:
        with tempfile.TemporaryDirectory(prefix="resolve-no-hooks-") as empty:
            return subprocess.run(  # noqa: S603
                [git, "-c", f"core.hooksPath={empty}", *args], check=False, timeout=timeout
            ).returncode
    try:
        return _commit_approved(git, args, files, timeout, approval)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"resolve hooks: {exc} — no further commit attempted", file=sys.stderr)
        return 1


def _index_entries(git: str) -> dict[str, bytes]:
    """Capture staged object identities so hooks cannot widen a recovery commit through index-only changes."""
    return {
        os.fsdecode(entry.split(b"\t", 1)[1]): entry.split(b"\t", 1)[0]
        for entry in _git_output(git, "ls-files", "--stage", "-z").split(b"\0")
        if entry
    }


def _commit_approved(git: str, args: list[str], files: list[str] | None, timeout: int, approval: str) -> int:
    """Validate the displayed approval and recover only a formatter's selected-file edits."""
    if inspect_repository_hooks(git)["sha256"] != approval:
        raise RuntimeError("hook commands/configuration changed or approval is invalid; show them and ask again")
    selected = files
    if selected is None:
        raw = _git_output(git, "diff", "--cached", "--name-only", "--no-renames", "-z")
        selected = [os.fsdecode(name) for name in raw.split(b"\0") if name]
    before = _working_files(git)
    staged_before = _index_entries(git)
    result = subprocess.run([git, *args], check=False, timeout=timeout)  # noqa: S603
    if result.returncode == 0:
        return 0
    after = _working_files(git)
    staged_after = _index_entries(git)
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
    if inspect_repository_hooks(git)["sha256"] != approval:
        raise RuntimeError("hook commands/configuration changed during execution; show them and ask again")
    subprocess.run(  # noqa: S603
        [git, "--literal-pathspecs", "add", "--", *sorted(changed)], check=True, timeout=_PLUMBING_TIMEOUT
    )
    print("resolve hooks: restaged hook-modified selected files; retrying commit once", file=sys.stderr)
    retry = subprocess.run([git, *args], check=False, timeout=timeout)  # noqa: S603
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
    action.add_argument("--remember", choices=[mode.value for mode in PolicyMode], help="Save a per-repository choice.")
    action.add_argument("--forget", action="store_true", help="Remove the saved per-repository choice.")
    parser.add_argument("--base", help="Base reference the PR is compared with; required by --decide.")
    parser.add_argument(
        "--approved-file", help="With --decide: write the digest (run) or an empty file (none, disabled)."
    )
    args = parser.parse_args(argv)
    if args.decide and not args.base:
        parser.error("--decide requires --base")
    try:
        if args.commit_merge:
            _git_output("git", "rev-parse", "--verify", "MERGE_HEAD")
            return commit_with_hooks("git", ["commit", "--no-edit"], None, 600)
        if args.decide:
            _print_decision(decide_hooks("git", args.base), args.approved_file)
        elif args.remember:
            print(f"resolve hooks: saved '{args.remember}' in {save_policy('git', PolicyMode(args.remember))}")
        elif args.forget:
            _policy_path("git").unlink(missing_ok=True)
        else:
            print(json.dumps(inspect_repository_hooks("git"), ensure_ascii=True, indent=2))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"resolve hooks: {'merge commit' if args.commit_merge else 'inspection'} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
