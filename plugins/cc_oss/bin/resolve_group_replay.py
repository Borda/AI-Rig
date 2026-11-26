#!/usr/bin/env python
"""Commit one topic group of an ``oss:resolve`` grouped run from its own items' changes, splitting shared files.

Purpose: In grouped commit mode the merge-back collapses every implemented item into one staged diff, then commits one
group at a time. Committing a group by file name took whole files, so when two groups changed the same file the first
group's commit carried the other group's hunks too, and a later group whose files were all taken had nothing left to
commit. This helper builds each group's commit in a private index instead. A file that an item of a group not yet
committed also changed is split by hunk: the group's own recorded item commits are replayed onto the current branch
head for that file. Every other file — one no other group touches, or one whose other items are already committed —
goes in whole from the collapsed target, as before. Each group keeps one commit with only its own changes, and the
last group to touch a file lands exactly the target's content.

Scope: Works on the run's own records in ``--impl-dir``: ``phase2-commits.jsonl`` (item id to implementation commit)
and ``c1-item-files.tsv`` (files of items implemented directly, which have no commit). The group's files are derived
from those records (``git diff-tree -z`` for commits, the raw record for direct items), never from a displayed path
list, so every path reaches git byte for byte. The collapsed staged tree is saved once as the target
(``group-replay-target.txt``) and is what every group converges to. The working tree is never written; only the index
changes, and it is put back to the target after settlement, so the files of later groups stay staged. Repository
hooks run under the same approval boundary as every other resolve commit (``resolve_commit_hooks``).

Directly implemented items: they have no commit, so their files come from the working tree, as the earlier
``git add`` did. When such a file also holds the change of an item of a later group, that item's recorded commit is
reverse-applied to separate it, so the direct item's group still commits only its own change.

Fallback: Shared files are taken whole when changes cannot be separated or repository hooks are approved. An
approved formatter can then rewrite adjacent lines without colliding with later groups' unstaged hunks. The
``GROUP_REPLAY_FALLBACK=`` line names later items only when their changes are proven in the committed file. A
later group whose items landed on all their paths reports success without a separate commit. A receipt per topic
records the commit, recovery tree and item ids before reconciliation or updates to ``group-replay-committed.txt``.
Close-out and later groups can recover those ids if the fence or the ledger update was interrupted.

Git version: splitting uses ``git apply --cached --3way``, which git accepts from version 2.32. An older or
unrecognised git applies without the three-way merge; a change that then does not apply cleanly falls back as above,
and the fallback line says the three-way merge needs git 2.32 or later.

Time budget: commit preparation and the commit share :data:`BUDGET_SECONDS`. A successful commit starts a separate
bookkeeping budget. The HEAD check after a commit error and the two index-restore calls have their own caps. The
fence must exceed :data:`WORST_CASE_SECONDS`.

Usage:
    resolve_group_replay.py --impl-dir DIR --topic T --summaries-file F --pr REF --items "1 3" [--codex]

Exit codes:
    0 — the group committed and settled, or every one of its items already landed in a confirmed commit
    1 — replay blocked without a confirmed group commit
    3 — the group's items are recorded as committed or change nothing against HEAD; no additional commit created
    4 — the group committed, but the commit command or reconciliation reported an error; the pre-hook target is printed

Used by: ``skills/resolve/modes/dispatch-phase3.md`` (grouped commit fence).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from shutil import which
from tempfile import NamedTemporaryFile

from commit_action_item import GroupMessageFields, build_group_message
from resolve_commit_hooks import CallBudget, commit_with_hooks

#: Seconds allowed for each plumbing call (read-tree, apply, write-tree), within the shared budget.
_PLUMBING_TIMEOUT = 60
#: Seconds shared by every git call up to and including the commit.
BUDGET_SECONDS = 400
#: Independent budget for proving absorption, refreshing the target and reading the committed head.
_POST_COMMIT_SECONDS = 120
#: Seconds allowed for each HEAD check or index-restore call after the shared budget expires.
_AFTER_BUDGET_TIMEOUT = 20
#: Upper bound including the HEAD check, post-commit bookkeeping and the two restore calls.
WORST_CASE_SECONDS = BUDGET_SECONDS + _POST_COMMIT_SECONDS + 3 * _AFTER_BUDGET_TIMEOUT
#: First git version that accepts ``apply --3way`` together with ``--cached``.
MIN_THREE_WAY_GIT = (2, 32)
#: File holding the target tree id: the collapsed staged diff every group converges to.
TARGET_FILE = "group-replay-target.txt"
#: Record of files taken whole, one line each, read back by the final report.
FALLBACK_RECORD = "group-replay-fallbacks.txt"
#: Record of item ids already committed by earlier groups of this run, one per line.
COMMITTED_RECORD = "group-replay-committed.txt"
#: Topic-specific receipt path, also read by the grouped close-out fence.
_RECEIPT_FILE = "group-replay-receipt-{}.txt"
_ATTEMPT_FILE = "group-replay-attempt-{}.json"
_FENCE_FILE = "group-replay-fence-{}.json"
#: Record of later-group item changes that landed in an earlier group's commit, one JSON object per line.
ABSORBED_RECORD = "group-replay-absorbed.jsonl"
#: Private index used to build one group's tree without touching the real index.
_SCRATCH_INDEX = "group-replay.index"
#: Commit message file written for each group.
_MESSAGE_FILE = "group-replay-message.txt"
#: Exit code for a group whose items change nothing against the current head.
EXIT_NOTHING = 3
#: Exit code when the group committed and post-commit reconciliation blocked.
EXIT_COMMITTED_BLOCKED = 4
#: A full or abbreviated hexadecimal object id.
_HEX = re.compile(r"^[0-9a-f]{7,64}$")
#: Abbreviated hexadecimal object id length kept in printed output.
_SHORT = 12
#: Leading ``major.minor`` of ``git version`` output.
_GIT_VERSION = re.compile(r"git version (\d+)\.(\d+)")
#: Separator between a commit message body and its trailers, as the group message builder writes it.
_TRAILER_SEPARATOR = "\n---\n"


class ReplayError(Exception):
    """A required record or Git operation failed."""


class CommittedBlocked(ReplayError):
    """The group committed and its pending edits require reconciliation."""


@dataclass(frozen=True)
class GroupRequest:
    """One group's commit request, as the grouped commit fence passes it.

    Attributes:
        impl_dir: The run's working directory holding its records.
        topic: Topic label of the group.
        summaries: Per-item commit subjects, in group order.
        pr: Pre-formatted PR reference for the commit message.
        items: Item ids of the group, in order.
        codex: Whether a directly implemented item adds the Codex co-author trailer.
    """

    impl_dir: Path
    topic: str
    summaries: tuple[str, ...]
    pr: str
    items: tuple[str, ...]
    codex: bool
    fence: str = ""


@dataclass(frozen=True)
class CommitReceipt:
    """Identify a group's landed commit and whether its pending edits were reconciled."""

    head: str
    target: str
    items: tuple[str, ...]
    settled: bool = False
    tree: str = ""
    proof_pending: bool = False

    @classmethod
    def read(cls, path: Path) -> CommitReceipt:
        """Read one receipt, refusing incomplete or unrecognized records."""
        lines = path.read_text(encoding="utf-8").splitlines()
        header = lines[0].split() if lines else []
        items = tuple(lines[1:])
        if len(header) not in {5, 6} or header[0] != "commit" or header[2] != "target":
            raise ReplayError(f"invalid group commit receipt: {path.name}")
        _, head, _, target, state, *tree = header
        if not (all(_HEX.fullmatch(oid) for oid in (head, target, *tree)) and all(item.isdigit() for item in items)):
            raise ReplayError(f"invalid commit, target or items in group receipt: {path.name}")
        if state not in {"attributed", "pending", "settled"} or (state == "attributed" and (items or not tree)):
            raise ReplayError(f"invalid reconciliation state in group receipt: {path.name}")
        return cls(head, target, items, state == "settled", next(iter(tree), ""), state == "attributed")

    def write(self, path: Path) -> None:
        """Publish a complete receipt even if the fence stops during a later write."""
        state = "attributed" if self.proof_pending else "settled" if self.settled else "pending"
        header = f"commit {self.head} target {self.target} {state} {self.tree}".rstrip()
        _atomic_write(path, "\n".join((header, *self.items, "")))


@dataclass(frozen=True)
class CommitAttempt:
    """Preserve the identity and expected tree before calling Git or repository hooks."""

    before: str
    target: str
    tree: str
    files: tuple[str, ...]
    items: tuple[str, ...]
    message: str
    fallbacks: tuple[Fallback, ...] = ()

    @classmethod
    def read(cls, path: Path) -> CommitAttempt:
        """Load a complete attempt recorded before committing."""
        row = json.loads(path.read_text(encoding="utf-8"))
        fallbacks = tuple(Fallback(f["path"], f["item"], f["reason"], tuple(f["absorbed"])) for f in row["fallbacks"])
        return cls(
            row["before"],
            row["target"],
            row["tree"],
            tuple(row["files"]),
            tuple(row["items"]),
            row["message"],
            fallbacks,
        )


def _atomic_write(path: Path, content: str) -> None:
    """Publish a record in one replacement so an interrupted reader sees a complete version."""
    with NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8", newline="\n") as temp:
        staged = Path(temp.name)
        temp.write(content)
    try:
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


@dataclass(frozen=True)
class Fallback:
    """A file the group took whole instead of splitting it.

    Attributes:
        path: Repository-relative path.
        item: The group's item whose change could not be split out.
        reason: Why the split failed, in plain words.
        absorbed: Later-group items whose change on ``path`` the whole file also carries, in id order.
    """

    path: str
    item: str
    reason: str
    absorbed: tuple[str, ...] = ()

    def line(self, topic: str) -> str:
        """Return the ``GROUP_REPLAY_FALLBACK=`` text for this file.

        Examples:
            >>> Fallback("core.py", "1", "recorded change no longer applies").line("logic")
            'core.py (item 1, group logic: recorded change no longer applies; file taken whole)'
            >>> Fallback("core.py", "1", "x", ("2", "5")).line("logic")
            'core.py (item 1, group logic: x; file taken whole; also carries items 2 5 of a later group)'
        """
        carried = f"; also carries items {' '.join(self.absorbed)} of a later group" if self.absorbed else ""
        return f"{self.path} (item {self.item}, group {topic}: {self.reason}; file taken whole{carried})"


@dataclass
class ReplayResult:
    """What building one group's tree did.

    Attributes:
        tree: The group's tree id.
        files: Every file the group's items changed, in first-seen order (the paths hooks may touch).
        fallbacks: Files taken whole instead of split.
    """

    tree: str
    files: tuple[str, ...] = ()
    fallbacks: list[Fallback] = field(default_factory=list)


@dataclass
class RunRecords:
    """The run's implementation records, as one group sees them.

    Attributes:
        commits: ``{item_id: sha}`` in ledger order (each worktree's commit order).
        files: ``{item_id: [path, ...]}`` for every item with a commit or a direct-implementation record.
        committed: Item ids earlier groups of this run committed.
        absorbed: ``{path: {item_id}}``: later-group changes that already landed whole in an earlier group's commit.
        absorbed_by: ``{item_id: {topic}}``: the groups whose commits carry an item's absorbed change.
    """

    commits: dict[str, str]
    files: dict[str, list[str]]
    committed: set[str]
    absorbed: dict[str, set[str]] = field(default_factory=dict)
    absorbed_by: dict[str, set[str]] = field(default_factory=dict)

    def owners(self, path: str) -> set[str]:
        """Return the items whose recorded changes touch ``path``."""
        return {item for item, paths in self.files.items() if path in paths}


class Git:
    """Run git commands in the current repository under one time budget, optionally against a private index.

    Attributes:
        executable: The git executable.
        budget: Time shared by every call made without an explicit timeout.
        three_way: Whether ``apply --cached --3way`` is available (git 2.32 or later).
        version: The ``git version`` text, for messages.
    """

    def __init__(self, executable: str, budget: CallBudget, version: str = "", three_way: bool = False) -> None:
        """Bind the git executable, its time budget and its capabilities."""
        self.executable = executable
        self.budget = budget
        self.version = version
        self.three_way = three_way

    def run(
        self, *args: str, index: Path | None = None, stdin: bytes | None = None, timeout: float | None = None
    ) -> bytes:
        """Run one git command and return its stdout; raise :class:`ReplayError` on a non-zero exit.

        Args:
            *args: Git arguments.
            index: Private index file; ``None`` uses the repository index.
            stdin: Bytes fed to the command.
            timeout: Explicit bound for a call that runs after the budget; ``None`` draws from the budget.

        Returns:
            Raw stdout.
        """
        proc = self.attempt(*args, index=index, stdin=stdin, timeout=timeout)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise ReplayError(f"git {args[0]} failed (exit {proc.returncode}): {detail[-1] if detail else ''}")
        return proc.stdout

    def attempt(
        self, *args: str, index: Path | None = None, stdin: bytes | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess:
        """Run one git command and return the completed process whatever its exit code.

        Raises:
            subprocess.TimeoutExpired: When the call outlives its bound, or the budget is already spent.
        """
        env = dict(os.environ)
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        return subprocess.run(  # noqa: S603 - argv list, no shell
            [self.executable, "--literal-pathspecs", *args],
            input=stdin,
            capture_output=True,
            timeout=self.budget.cap(_PLUMBING_TIMEOUT) if timeout is None else timeout,
            check=False,
            env=env,
        )

    def text(self, *args: str, index: Path | None = None, timeout: float | None = None) -> str:
        """Run one git command and return its stripped stdout as text."""
        return self.run(*args, index=index, timeout=timeout).decode("utf-8", "replace").strip()


def git_version(text: str) -> tuple[int, int] | None:
    """Return ``(major, minor)`` from ``git version`` output, or ``None`` when it is not recognised.

    Examples:
        >>> git_version("git version 2.54.0")
        (2, 54)
        >>> git_version("git version 2.45.1.windows.1")
        (2, 45)
        >>> git_version("git version 2.39.3 (Apple Git-146)")
        (2, 39)
        >>> git_version("unexpected") is None
        True
    """
    match = _GIT_VERSION.search(text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def supports_three_way(text: str) -> bool:
    """Tell whether a git with this ``git version`` output accepts ``apply --cached --3way``.

    An unrecognised version counts as unsupported: the plain apply it falls back to works on every version.

    Examples:
        >>> supports_three_way("git version 2.32.0"), supports_three_way("git version 2.31.8")
        (True, False)
        >>> supports_three_way("git version 3.0.0"), supports_three_way("")
        (True, False)
    """
    version = git_version(text)
    return version is not None and version >= MIN_THREE_WAY_GIT


def load_item_commits(impl_dir: Path) -> dict[str, str]:
    """Map item ids to their implementation commit from ``phase2-commits.jsonl``.

    Args:
        impl_dir: The run's working directory.

    Returns:
        ``{item_id: sha}``; an absent ledger gives an empty map.

    Raises:
        ReplayError: When a row is not JSON or names a non-hexadecimal commit.
    """
    ledger = impl_dir / "phase2-commits.jsonl"
    if not ledger.is_file():
        return {}
    commits: dict[str, str] = {}
    for number, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayError(f"phase2-commits.jsonl line {number} is not JSON: {exc}") from exc
        item, sha = str(row.get("item_id", "")), str(row.get("sha", ""))
        if not _HEX.match(sha):
            raise ReplayError(f"phase2-commits.jsonl line {number}: commit '{sha}' is not hexadecimal")
        commits.setdefault(item, sha)
    return commits


def load_direct_files(impl_dir: Path) -> dict[str, list[str]]:
    """Map item ids to the files recorded for items implemented directly (no commit).

    Args:
        impl_dir: The run's working directory.

    Returns:
        ``{item_id: [file, ...]}`` from ``c1-item-files.tsv``; an absent file gives an empty map.
    """
    record = impl_dir / "c1-item-files.tsv"
    files: dict[str, list[str]] = {}
    if not record.is_file():
        return files
    # The producer writes LF-delimited UTF-8. Universal newlines and splitlines can split inside a path.
    for line in record.read_bytes().decode("utf-8").split("\n"):
        item, _, path = line.partition("\t")
        if item.strip().isdigit() and path:
            files.setdefault(item.strip(), []).append(path)
    return files


def _committed_items(impl_dir: Path) -> set[str]:
    """Return item ids confirmed by the commit ledger or a receipt written before it."""
    record = impl_dir / COMMITTED_RECORD
    lines = record.read_text(encoding="utf-8").splitlines() if record.is_file() else []
    committed = {line.strip() for line in lines if line.strip()}
    for receipt in impl_dir.glob(_RECEIPT_FILE.format("*")):
        committed.update(CommitReceipt.read(receipt).items)
    return committed


def _absorption_rewrites(git: Git, impl_dir: Path, topic: str, head: str) -> set[str] | None:
    """Locate paths changed since the reachable receipt that proved an absorption."""
    path = impl_dir / _RECEIPT_FILE.format(topic)
    if not path.is_file():
        return None
    receipt = CommitReceipt.read(path)
    if receipt.proof_pending or git.attempt("merge-base", "--is-ancestor", receipt.head, head).returncode != 0:
        return None
    return _different_paths(git, receipt.tree or receipt.head, head)


def _load_absorbed(git: Git, impl_dir: Path, records: RunRecords, head: str) -> None:
    """Reuse absorption proof only while the earlier commit and its exact path entry still hold."""
    record = impl_dir / ABSORBED_RECORD
    if not record.is_file():
        return
    rewrites: dict[str, set[str] | None] = {}
    for number, line in enumerate(record.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayError(f"{ABSORBED_RECORD} line {number} is not JSON: {exc}") from exc
        item, path, topic = str(row.get("item", "")), str(row.get("path", "")), str(row.get("group", ""))
        if not re.fullmatch(r"[a-z0-9-]+", topic):
            continue
        if topic not in rewrites:
            rewrites[topic] = _absorption_rewrites(git, impl_dir, topic, head)
        changed = rewrites[topic]
        if changed is None or path in changed:
            continue
        records.absorbed.setdefault(path, set()).add(item)
        records.absorbed_by.setdefault(item, set()).add(topic)


def _item_files(git: Git, sha: str) -> list[str]:
    """List the files one implementation commit changed, renames split into delete and add, byte for byte."""
    raw = git.run("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--no-renames", "--root", sha)
    return [os.fsdecode(name) for name in raw.split(b"\0") if name]


def load_records(git: Git, impl_dir: Path, *, head: str = "HEAD") -> RunRecords:
    """Read every implementation record of the run.

    Args:
        git: Git runner.
        impl_dir: The run's working directory.
        head: Commit at which earlier absorptions must still be present.

    Returns:
        The run's records as the next group sees them.
    """
    commits = load_item_commits(impl_dir)
    files = {item: _item_files(git, sha) for item, sha in commits.items()} | load_direct_files(impl_dir)
    records = RunRecords(commits=commits, files=files, committed=_committed_items(impl_dir))
    _load_absorbed(git, impl_dir, records, head)
    return records


def _take_whole(git: Git, index: Path, target: str, path: str) -> None:
    """Set ``path`` in the private index to its target content, or remove it when the target lacks it."""
    listing = git.run("ls-tree", "-z", "--full-tree", target, "--", path)
    null_oid = "0" * len(target)
    feed = f"0 {null_oid}\t{path}\0".encode(errors="surrogateescape") + listing
    git.run("update-index", "-z", "--index-info", index=index, stdin=feed)


def _take_working(git: Git, index: Path, path: str) -> None:
    """Set ``path`` in the private index to its working-tree content, or remove it when the file is gone.

    Directly implemented items have no commit to replay; their edit lives in the working tree (staged after the merge-
    back, or still unstaged), exactly what the whole-file group commit used to take.
    """
    git.run("update-index", "--add", "--remove", "--", path, index=index)


def _item_patch(git: Git, sha: str, path: str) -> bytes:
    """Read one item's patch with its exact preimage and postimage blob IDs."""
    return git.run(
        "diff-tree", "-p", "--binary", "--full-index", "--no-renames", "--no-commit-id", "--root", "-r", sha, "--", path
    )


def _apply_file(git: Git, index: Path, sha: str, path: str, *, reverse: bool = False) -> bool:
    """Apply, or with ``reverse`` undo, one file's change from one implementation commit in the private index.

    Returns:
        ``True`` when the change applied (three-way when available and the context moved), ``False`` when it did not.
    """
    patch = _item_patch(git, sha, path)
    if not patch.strip():
        return True
    options = ["--cached", "--whitespace=nowarn", *(["--3way"] if git.three_way else []), *(["-R"] if reverse else [])]
    return git.attempt("apply", *options, "-", index=index, stdin=patch).returncode == 0


def _sorted_ids(items: set[str]) -> tuple[str, ...]:
    """Return item ids in numeric order (ids are digits by contract; anything else sorts last)."""
    return tuple(sorted(items, key=lambda item: (not item.isdigit(), int(item) if item.isdigit() else 0, item)))


@dataclass(frozen=True)
class _GroupPlan:
    """Inputs one group's tree is built from.

    Attributes:
        items: The group's items with a record, in replay order (ledger order, then directly implemented items).
        records: The run's records.
        target: The target tree id.
        settled: Items whose changes are in this commit or an earlier one: the group's own and earlier groups'.
    """

    items: tuple[str, ...]
    records: RunRecords
    target: str
    settled: frozenset[str]
    approved_hooks: bool

    def unsettled(self, path: str) -> set[str]:
        """Return the later-group items whose change on ``path`` is still staged and not yet in any commit."""
        return self.records.owners(path) - self.settled - self.records.absorbed.get(path, set())


def _replay_reason(git: Git) -> str:
    """Say why a recorded change did not split, naming the git version when the three-way merge was unavailable."""
    if git.three_way:
        return "recorded change no longer applies"
    found = git.version or "of unknown version"
    return (
        "recorded change does not apply cleanly, and a three-way merge in the index needs git "
        f"{MIN_THREE_WAY_GIT[0]}.{MIN_THREE_WAY_GIT[1]} or later ({found} found)"
    )


def _place_direct(git: Git, index: Path, plan: _GroupPlan, path: str, item: str) -> Fallback | None:
    """Stage a directly implemented item's file, separating any later group's change from it.

    The working file holds the direct item's edit plus every other staged change to that file. Each later-group item's
    recorded commit is reverse-applied, so only this group's changes remain. A later item implemented directly has no
    commit to undo, so the file then goes in whole and that item is named as absorbed.
    """
    _take_working(git, index, path)
    unsettled = plan.unsettled(path)
    for other in _sorted_ids(unsettled):
        sha = plan.records.commits.get(other)
        if sha is None:
            reason = f"item {other} of a later group is also implemented directly, so its change cannot be separated"
        elif _apply_file(git, index, sha, path, reverse=True):
            continue
        else:
            reason = f"item {other}'s recorded change could not be separated from the working file"
        _take_working(git, index, path)  # undo any partial separation
        return Fallback(path, item, reason, _sorted_ids(unsettled))
    return None


def _place_committed(
    git: Git, index: Path, plan: _GroupPlan, path: str, own: list[str], unsettled: set[str]
) -> Fallback | None:
    """Replay commit-backed changes while other groups still have work on the path."""
    for item in own:
        if not _apply_file(git, index, plan.records.commits[item], path):
            _take_whole(git, index, plan.target, path)
            return Fallback(path, item, _replay_reason(git), _sorted_ids(unsettled))
    return None


def _place_path(git: Git, index: Path, plan: _GroupPlan, path: str, result: ReplayResult) -> None:
    """Stage one of the group's files in the private index: split by hunk, or whole when nothing else is pending.

    A file taken whole is recorded in ``result.fallbacks``. A group item whose change to the file already landed whole
    in an earlier group's commit is skipped.
    """
    landed = plan.records.absorbed.get(path, set())
    own = [item for item in plan.items if path in plan.records.files.get(item, []) and item not in landed]
    if not own:
        return
    unsettled = plan.unsettled(path)
    direct = [item for item in own if item not in plan.records.commits]
    if unsettled and plan.approved_hooks:
        # Hooks such as pre-commit may roll back a formatter fix if it touches the context of an unstaged hunk.
        if direct:
            _take_working(git, index, path)
        else:
            _take_whole(git, index, plan.target, path)
        found = Fallback(path, own[0], "approved hooks need the complete file", _sorted_ids(unsettled))
    elif direct:
        found = _place_direct(git, index, plan, path, direct[0])
    elif unsettled:
        found = _place_committed(git, index, plan, path, own, unsettled)
    else:
        _take_whole(git, index, plan.target, path)
        return
    if found is not None:
        result.fallbacks.append(found)


def build_group_tree(git: Git, request: GroupRequest, target: str, records: RunRecords) -> ReplayResult:
    """Replay the group's items onto the current head in a private index and write the resulting tree.

    Split shared files by hunk when hooks are disabled and separation is possible. Whole files preserve the staged
    target, or the working file for directly implemented items.

    Args:
        git: Git runner.
        request: The group's request.
        target: The target tree id.
        records: The run's records.

    Returns:
        The group's tree, its files, and the files taken whole.
    """
    index = request.impl_dir / _SCRATCH_INDEX
    index.unlink(missing_ok=True)
    git.run("read-tree", "HEAD", index=index)
    # ledger order is each worktree's commit order, so items chained in one worktree replay in the order they were built
    ordered = [item for item in records.commits if item in request.items]
    ordered += [item for item in request.items if item not in records.commits and item in records.files]
    plan = _GroupPlan(
        tuple(ordered),
        records,
        target,
        frozenset(request.items) | records.committed,
        bool(os.environ.get("RESOLVE_APPROVED_HOOKS")),
    )
    result = ReplayResult(tree="", files=tuple(dict.fromkeys(path for item in ordered for path in records.files[item])))
    for path in result.files:
        _place_path(git, index, plan, path, result)
    result.tree = git.text("write-tree", index=index)
    index.unlink(missing_ok=True)
    return result


def load_target(git: Git, impl_dir: Path) -> str:
    """Return the target tree, saving the current index as the target on the first group.

    Args:
        git: Git runner.
        impl_dir: The run's working directory.

    Returns:
        The target tree id.

    Raises:
        ReplayError: When the index no longer equals the saved target (something else changed it between groups).
    """
    record = impl_dir / TARGET_FILE
    current = git.text("write-tree")
    if not record.is_file():
        record.write_text(current + "\n", encoding="utf-8", newline="\n")
        return current
    target = record.read_text(encoding="utf-8").strip()
    if current != target:
        raise ReplayError(
            f"the staged tree {current[:_SHORT]} differs from the replay target {target[:_SHORT]}: the index changed "
            f"between groups; restore it with `git read-tree {target}` before committing the next group"
        )
    return target


def _refresh_target(
    git: Git, impl_dir: Path, target: str, head_tree: str, files: set[str], pending: dict[str, dict[str, str | None]]
) -> tuple[str, list[str], dict[str, set[str]]]:
    """Refresh the target and report unresolved paths and items already present in HEAD."""
    if not files:
        return target, [], {}
    index = impl_dir / _SCRATCH_INDEX
    _reset_scratch(git, index, target)
    unresolved = []
    absorbed: dict[str, set[str]] = {}
    for path in sorted(files):
        _take_whole(git, index, head_tree, path)
        still_at_head = True
        for item, sha in pending.get(path, {}).items():
            if sha is None or not _apply_file(git, index, sha, path):
                # Failed three-way applies can leave conflicts. Keep the committed file for recovery.
                _take_whole(git, index, head_tree, path)
                unresolved.append(path)
                break
            # A no-op proves absorption only while the path still consists solely of committed content.
            diff = ("diff", "--cached", "--raw", "--ignore-submodules=none", head_tree, "--", path)
            still_at_head = still_at_head and not git.run(*diff, index=index)
            if still_at_head:
                absorbed.setdefault(path, set()).add(item)
    refreshed = git.text("write-tree", index=index)
    (impl_dir / TARGET_FILE).write_text(refreshed + "\n", encoding="utf-8", newline="\n")
    return refreshed, unresolved, absorbed


def _reset_scratch(git: Git, index: Path, tree: str) -> None:
    """Load one tree into a fresh private index, discarding any failed patch's conflicts."""
    index.unlink(missing_ok=True)
    git.run("read-tree", tree, index=index)


def _restore_index(git: Git, tree: str) -> None:
    """Put the real index back to ``tree`` and refresh its stat data, outside the spent-or-not budget."""
    git.run("read-tree", tree, timeout=_AFTER_BUDGET_TIMEOUT)
    git.attempt("update-index", "-q", "--refresh", timeout=_AFTER_BUDGET_TIMEOUT)


def _attributed_head(git: Git, attempt: CommitAttempt) -> tuple[str, str] | None:
    """Identify a direct child by its raw commit message and the scope of any staged rewrite."""
    head = git.text("rev-parse", "--verify", "HEAD")
    if head == attempt.before:
        return None
    header, _, body = git.run("cat-file", "commit", head).partition(b"\n\n")
    parents = [line[7:] for line in header.split(b"\n") if line.startswith(b"parent ")]
    tree = header.split(b"\n", 1)[0].removeprefix(b"tree ").decode("ascii")
    if (
        parents != [attempt.before.encode("ascii")]
        or body != attempt.message.encode("utf-8")
        or not _different_paths(git, attempt.tree, tree).issubset(attempt.files)
    ):
        raise ReplayError(f"HEAD {head} does not match the recorded parent, message and tree scope; inspect HEAD")
    return head, tree


def _commit_index(git: Git, attempt: CommitAttempt, message: Path) -> tuple[int, str, tuple[str, str] | None]:
    """Commit the staged tree, restoring the target only when HEAD stayed put.

    Returns:
        The replay exit code, any commit error and the attributed commit id.
    """
    restore = True
    try:
        git.run("read-tree", attempt.tree)
        expected_tree = git.text("write-tree")
        if expected_tree != attempt.tree:
            raise ReplayError(f"index tree changed to {expected_tree} before commit; expected {attempt.tree}")
        commit = ["--literal-pathspecs", "commit", "--cleanup=verbatim", "-F", str(message)]
        restore = False
        try:
            code = commit_with_hooks(git.executable, commit, list(attempt.files), git.budget.remaining())
        except subprocess.TimeoutExpired:
            code, outcome = 1, "the commit command timed out"
        except (OSError, subprocess.SubprocessError) as exc:
            code, outcome = 1, f"the commit command failed ({exc})"
        else:
            outcome = f"the commit command returned exit {code}"

        # Git updates HEAD before running post-commit hooks, so an error can arrive after the commit exists.
        git.budget = CallBudget.starting_now(_AFTER_BUDGET_TIMEOUT)
        observed = _attributed_head(git, attempt)
        if observed is None:
            restore = True
            return 1, f"{outcome} before HEAD advanced — index restored", None
        if code == 0:
            return 0, "", observed
        return EXIT_COMMITTED_BLOCKED, f"{outcome} after HEAD advanced to {observed[0][:_SHORT]}", observed
    finally:
        if restore:
            try:
                _restore_index(git, attempt.target)
            except (ReplayError, subprocess.SubprocessError) as exc:
                raise ReplayError(f"index not restored ({exc}); run `git read-tree {attempt.target}`") from exc


def _message(request: GroupRequest, fallbacks: list[Fallback], *, hooks: bool) -> str:
    """Build the group's commit message, naming any later-group change a whole file carries into it."""
    fields = GroupMessageFields(request.topic, request.summaries, request.pr, " ".join(request.items), request.codex)
    # A pre-hook message records requested work. Only the receipt can confirm what reached the committed tree.
    message = build_group_message(fields).replace("\n[resolve group]", "\n[resolve group attempt]", 1)
    carried = [found for found in fallbacks if found.absorbed]
    if not carried:
        return message
    note = "; ".join(f"{found.path} (items {' '.join(found.absorbed)})" for found in carried)
    body, separator, trailers = message.rpartition(_TRAILER_SEPARATOR)
    if hooks:
        line = "Shared files staged whole for approved hooks: " + "; ".join(found.path for found in carried) + "\n"
    else:
        line = f"Also carries later-group changes in files taken whole: {note}\n"
    return f"{body}{line}{separator}{trailers}" if separator else message + "\n" + line


def _append(path: Path, lines: list[str]) -> None:
    """Append LF-terminated lines to a run record."""
    if lines:
        with path.open("a", encoding="utf-8", newline="\n") as record:
            record.write("".join(f"{line}\n" for line in lines))


def _record_commit(impl_dir: Path, topic: str, receipt: CommitReceipt, fallbacks: list[Fallback]) -> None:
    """Record the committed items, the files taken whole and the later-group changes they carry."""
    _append(impl_dir / COMMITTED_RECORD, list(receipt.items))
    # durable for the final report, which may run after a compaction dropped this output
    _append(impl_dir / FALLBACK_RECORD, [found.line(topic) for found in fallbacks])
    rows = [
        json.dumps({"item": item, "path": found.path, "group": topic}) for found in fallbacks for item in found.absorbed
    ]
    _append(impl_dir / ABSORBED_RECORD, rows)


_ZERO_CONTEXT_DIFF = (
    "--unified=0",
    "--inter-hunk-context=0",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--no-renames",
)
_HUNK_HEADER = re.compile(rb"^@@ (-\d+(?:,\d+)?) \+\d+(?:,\d+)? @@[^\n]*\n", re.MULTILINE)


def _diff_hunks(patch: bytes) -> set[tuple[bytes, bytes]]:
    """Read text hunks keyed by their line range in the source blob."""
    _, *hunks = _HUNK_HEADER.split(patch)
    return set(zip(hunks[::2], hunks[1::2]))


def _item_reversed(git: Git, sha: str, path: str, built_tree: str, head_tree: str) -> bool:
    """Detect recorded hunks the hook explicitly reverted even when it also updated the working file."""
    unchanged = git.run("diff", "--raw", "--ignore-submodules=none", "--no-renames", f"{sha}^", head_tree, "--", path)
    if not unchanged:
        return True
    reverse = git.run("diff-tree", "-p", *_ZERO_CONTEXT_DIFF, "-R", "--no-commit-id", "--root", "-r", sha, "--", path)
    reverse_hunks = _diff_hunks(reverse)
    if not reverse_hunks:
        entry = ("ls-tree", "-z", "--full-tree")
        return git.run(*entry, sha, "--", path) != git.run(*entry, head_tree, "--", path)
    # Both diffs start at the item's postimage, so identical lines elsewhere cannot qualify as its reversal.
    diff = ("diff", *_ZERO_CONTEXT_DIFF, sha)
    reversed_in_head = reverse_hunks & _diff_hunks(git.run(*diff, head_tree, "--", path))
    return bool(reversed_in_head and reversed_in_head - _diff_hunks(git.run(*diff, built_tree, "--", path)))


def _kept_after_rewrite(git: Git, records: RunRecords, item: str, path: str, built: str, landed: str) -> bool:
    """Require identical direct entries and check commit-backed items for reversal after a rewrite."""
    sha = records.commits.get(item)
    return sha is not None and not _item_reversed(git, sha, path, built, landed)


def _prove_items(git: Git, attempt: CommitAttempt, tree: str, records: RunRecords) -> tuple[str, ...]:
    """Confirm each requested item using its paths in the attributed commit tree."""
    rewritten = _different_paths(git, attempt.tree, tree)
    changed = _different_paths(git, attempt.before, tree)
    confirmed = []
    for item in attempt.items:
        paths = records.files.get(item, [])
        if paths and all(
            (path not in changed or _kept_after_rewrite(git, records, item, path, attempt.before, tree))
            if item in records.absorbed.get(path, set())
            else path in changed
            and (path not in rewritten or _kept_after_rewrite(git, records, item, path, attempt.tree, tree))
            for path in paths
        ):
            confirmed.append(item)
    return tuple(confirmed)


def _prove_fallbacks(
    git: Git, head_tree: str, result: ReplayResult, records: RunRecords, rewritten: set[str]
) -> list[Fallback]:
    """Confirm which whole-file candidates actually landed, using the committed tree as the authority."""
    verified = []
    for found in result.fallbacks:
        path = found.path
        absorbed = found.absorbed
        if path in rewritten:
            # The built whole file held every candidate, even if the hook rewrote only the staged blob.
            absorbed = tuple(
                item for item in absorbed if _kept_after_rewrite(git, records, item, path, result.tree, head_tree)
            )
        verified.append(Fallback(found.path, found.item, found.reason, absorbed))
    return verified


def _different_paths(git: Git, *trees: str, index: Path | None = None) -> set[str]:
    """Return the exact names of changed tree or staged entries, including file modes and gitlinks."""
    raw = git.run("diff", "--name-only", "-z", "--no-renames", "--ignore-submodules=none", *trees, "--", index=index)
    return {os.fsdecode(name) for name in raw.split(b"\0") if name}


def _unchanged(request: GroupRequest, records: RunRecords) -> int:
    """Report a group whose tree equals the head: landed in an earlier group's commit, or nothing to commit."""
    if all(_fully_absorbed(item, records) for item in request.items):
        groups = sorted({topic for item in request.items for topic in records.absorbed_by[item]})
        _append(request.impl_dir / COMMITTED_RECORD, list(request.items))
        print(
            f"→ group '{request.topic}' — items {' '.join(request.items)} already landed in the commit of group(s) "
            f"{', '.join(groups)} (a shared file was taken whole there); no separate commit"
        )
        return 0
    print(f"resolve_group_replay: group '{request.topic}' changes nothing against HEAD — no commit created")
    return EXIT_NOTHING


def _fully_absorbed(item: str, records: RunRecords) -> bool:
    """Require a positive absorption record for every path of an item before closing its group."""
    paths = records.files.get(item, [])
    return bool(paths) and all(item in records.absorbed.get(path, set()) for path in paths)


def _pending_commits(before: Fallback, after: Fallback, records: RunRecords) -> dict[str, str | None]:
    """Order pending edits by implementation commit order and flag any that have no commit to replay."""
    pending = set(before.absorbed) - set(after.absorbed)
    commits: dict[str, str | None] = {item: sha for item, sha in records.commits.items() if item in pending}
    commits.update(dict.fromkeys(pending - commits.keys()))
    return commits


def _settle_after_commit(
    git: Git, request: GroupRequest, result: ReplayResult, receipt: CommitReceipt, records: RunRecords
) -> list[Fallback]:
    """Refresh the target and put the real index back to it once the group's commit exists.

    Keep unresolved hook changes committed and stop the run with the prior target available for recovery.
    """
    index = request.impl_dir / _SCRATCH_INDEX
    target = receipt.target
    verified: list[Fallback] = []
    try:
        head_tree = receipt.tree
        _reset_scratch(git, index, head_tree)
        git.run("update-index", "--add", "--remove", "--", *result.files, index=index)
        unstaged = _different_paths(git, "--cached", head_tree, index=index)
        rewritten = _different_paths(git, result.tree, head_tree) & set(result.files)
        verified = _prove_fallbacks(git, head_tree, result, records, rewritten)
        pending = {
            before.path: _pending_commits(before, after, records)
            for before, after in zip(result.fallbacks, verified)
            if before.absorbed != after.absorbed
        }
        refresh = rewritten | ((_different_paths(git, target, head_tree) & set(result.files)) - unstaged)
        refreshed, unresolved, absorbed = _refresh_target(git, request.impl_dir, target, head_tree, refresh, pending)
        verified = [
            Fallback(
                found.path, found.item, found.reason, _sorted_ids(set(found.absorbed) | absorbed.get(found.path, set()))
            )
            for found in verified
        ]
        _restore_index(git, refreshed)
    finally:
        _record_commit(request.impl_dir, request.topic, receipt, verified)
        try:
            index.unlink(missing_ok=True)
        except OSError as exc:
            print(f"⚠ resolve_group_replay: private index could not be removed ({exc})")
    if unresolved:
        raise CommittedBlocked(
            f"group '{request.topic}' committed, but the hook left pending edits that could not be replayed on "
            f"{', '.join(unresolved)!r}; compare HEAD and the pre-hook target {target} before continuing"
        )
    return verified


def _write_receipt(path: Path, attempt: CommitAttempt, observed: tuple[str, str]) -> CommitReceipt:
    """Publish the attributed commit even when later item verification cannot run."""
    head, tree = observed
    receipt = CommitReceipt(head, attempt.target, (), tree=tree, proof_pending=True)
    try:
        receipt.write(path)
    except OSError as exc:
        raise CommittedBlocked(f"commit {head} exists; could not save its receipt ({exc})") from exc
    return receipt


def _confirm_receipt(
    git: Git, path: Path, attempt: CommitAttempt, receipt: CommitReceipt, records: RunRecords
) -> CommitReceipt:
    """Attach only proven items to a durable commit receipt."""
    try:
        items = _prove_items(git, attempt, receipt.tree, records)
        receipt = replace(receipt, items=items, proof_pending=False)
        receipt.write(path)
    except (OSError, ReplayError, subprocess.SubprocessError) as exc:
        raise CommittedBlocked(f"commit {receipt.head} exists; could not record its landed items ({exc})") from exc
    return receipt


def _find_receipt(git: Git, impl_dir: Path, topic: str, items: tuple[str, ...]) -> CommitReceipt | None:
    """Read the group's receipt, or recover it from the earlier attempt after an interruption."""
    path = impl_dir / _RECEIPT_FILE.format(topic)
    attempt_path = impl_dir / _ATTEMPT_FILE.format(topic)
    attempt = CommitAttempt.read(attempt_path) if attempt_path.is_file() else None
    receipt = CommitReceipt.read(path) if path.is_file() else None
    original_items = attempt.items if attempt else receipt.items if receipt else items
    if set(original_items) != set(items):
        raise ReplayError(f"group '{topic}' has a commit record for different items")
    if attempt is None or (receipt is not None and not receipt.proof_pending):
        return receipt
    if receipt is None:
        observed = _attributed_head(git, attempt)
        if observed is None:
            return None
        receipt = _write_receipt(path, attempt, observed)
    try:
        records = load_records(git, impl_dir, head=attempt.before)
        receipt = _confirm_receipt(git, path, attempt, receipt, records)
        result = ReplayResult(attempt.tree, attempt.files, list(attempt.fallbacks))
        rewritten = _different_paths(git, attempt.tree, receipt.tree)
        _record_commit(impl_dir, topic, receipt, _prove_fallbacks(git, receipt.tree, result, records, rewritten))
    except (OSError, ReplayError, subprocess.SubprocessError) as exc:
        print(
            f"resolve_group_replay: commit {receipt.head} exists; incomplete recovery evidence ({exc})", file=sys.stderr
        )
    return receipt


def _write_fence(request: GroupRequest, *, landed: bool = False) -> None:
    """Bind close-out to this fence's validated group, including success without a new commit."""
    if request.fence:
        path = request.impl_dir / _FENCE_FILE.format(request.fence)
        _atomic_write(path, json.dumps([request.topic, request.items, landed]))


def _complete_commit(
    git: Git, request: GroupRequest, result: ReplayResult, receipt: CommitReceipt, records: RunRecords, failure: str
) -> int:
    """Settle a complete group or preserve its original target for recovery."""
    path = request.impl_dir / _RECEIPT_FILE.format(request.topic)
    unconfirmed = set(request.items) - set(receipt.items)
    try:
        if failure or unconfirmed:
            rewritten = _different_paths(git, result.tree, receipt.tree)
            verified = _prove_fallbacks(git, receipt.tree, result, records, rewritten)
            _record_commit(request.impl_dir, request.topic, receipt, verified)
            reason = failure or f"items {' '.join(_sorted_ids(unconfirmed))} could not be proven in the committed tree"
            raise CommittedBlocked(
                f"group '{request.topic}' committed, but {reason}; compare HEAD and the pre-hook target "
                f"{receipt.target} before continuing"
            )
        verified = _settle_after_commit(git, request, result, receipt, records)
        replace(receipt, settled=True).write(path)
    except CommittedBlocked:
        raise
    except (OSError, ReplayError, subprocess.SubprocessError) as exc:
        raise CommittedBlocked(
            f"group '{request.topic}' committed, but index reconciliation failed ({exc}); "
            f"inspect HEAD and the pre-hook target {receipt.target} before continuing"
        ) from exc
    for found in verified:
        print(f"GROUP_REPLAY_FALLBACK={found.line(request.topic)}")
    print(f"→ group '{request.topic}' committed {receipt.head[:_SHORT]} — items {' '.join(receipt.items)}")
    return 0


def commit_group(git: Git, request: GroupRequest) -> int:
    """Build and commit one group's tree, or report its previously attributed commit."""
    _write_fence(request)
    receipt = _find_receipt(git, request.impl_dir, request.topic, request.items)
    if receipt is not None:
        print(f"→ group '{request.topic}' already committed {receipt.head[:_SHORT]} — no additional commit")
        if not receipt.settled:
            raise CommittedBlocked(f"pending edits require reconciliation against pre-hook target {receipt.target}")
        return 0
    records = load_records(git, request.impl_dir)
    if records.committed.issuperset(request.items):
        print(f"resolve_group_replay: group '{request.topic}' is already recorded as committed — no additional commit")
        return EXIT_NOTHING
    target = load_target(git, request.impl_dir)
    result = build_group_tree(git, request, target, records)
    before = git.text("rev-parse", "--verify", "HEAD")
    if result.tree == git.text("rev-parse", f"{before}^{{tree}}"):
        code = _unchanged(request, records)
        if code == 0:
            _write_fence(request, landed=True)
        return code
    message = request.impl_dir / _MESSAGE_FILE
    hooks = bool(os.environ.get("RESOLVE_APPROVED_HOOKS"))
    body = _message(request, result.fallbacks, hooks=hooks) + "\n"
    attempt = CommitAttempt(before, target, result.tree, result.files, request.items, body, tuple(result.fallbacks))
    attempt_path = request.impl_dir / _ATTEMPT_FILE.format(request.topic)
    message.write_bytes(body.encode("utf-8"))
    _atomic_write(attempt_path, json.dumps(asdict(attempt)))
    code, failure, observed = _commit_index(git, attempt, message)
    if observed is None:
        attempt_path.unlink()
        print(f"resolve_group_replay: group '{request.topic}': {failure}")
        return code
    git.budget = CallBudget.starting_now(_POST_COMMIT_SECONDS)
    receipt_path = request.impl_dir / _RECEIPT_FILE.format(request.topic)
    receipt = _write_receipt(receipt_path, attempt, observed)
    receipt = _confirm_receipt(git, receipt_path, attempt, receipt, records)
    return _complete_commit(git, request, result, receipt, records, failure)


def _parse(argv: list[str] | None) -> GroupRequest:
    """Parse the command line into a :class:`GroupRequest`."""
    parser = argparse.ArgumentParser(description="Commit one resolve topic group from its own items' changes.")
    parser.add_argument("--impl-dir", required=True, help="the run's working directory holding its records")
    parser.add_argument("--topic", required=True, help="topic label of the group")
    parser.add_argument("--summaries-file", required=True, help="file with one item commit subject per line")
    parser.add_argument("--pr", required=True, help="PR reference for the commit message")
    parser.add_argument("--items", required=True, help="space-separated item ids of the group")
    parser.add_argument("--codex", action="store_true", help="add the Codex co-author trailer")
    parser.add_argument("--attempt-id", default="", help="bind close-out to the current fence")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9-]+", args.topic):
        parser.error("--topic must be lowercase alphanumeric/hyphen")
    if args.attempt_id and not re.fullmatch(r"[A-Za-z0-9-]+", args.attempt_id):
        parser.error("--attempt-id must be alphanumeric/hyphen")
    try:
        summaries = Path(args.summaries_file).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        parser.error(f"cannot read --summaries-file: {exc}")
    items = tuple(args.items.split())
    if not items or not all(item.isdigit() for item in items):
        parser.error("--items must be space-separated item ids")
    return GroupRequest(
        impl_dir=Path(args.impl_dir).resolve(),
        topic=args.topic,
        summaries=tuple(summaries),
        pr=args.pr,
        items=items,
        codex=args.codex,
        fence=args.attempt_id,
    )


def _open_git(executable: str) -> Git:
    """Start the run's budget and read the git version that decides whether three-way splitting is available."""
    git = Git(executable, CallBudget.starting_now(BUDGET_SECONDS))
    probe = git.attempt("version")
    git.version = probe.stdout.decode("utf-8", "replace").strip() if probe.returncode == 0 else ""
    git.three_way = supports_three_way(git.version)
    return git


def _close_out(git: Git, impl_dir: Path) -> int:
    """Print only the status and confirmed IDs of the current fence, recovering an interrupted commit if needed."""
    fence = (impl_dir / "group-commit-attempt.txt").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+", fence):
        raise ReplayError("invalid group fence id")
    path = impl_dir / _FENCE_FILE.format(fence)
    topic, items, landed = json.loads(path.read_text(encoding="utf-8"))
    if not re.fullmatch(r"[a-z0-9-]+", topic):
        raise ReplayError("invalid group fence topic")
    try:
        receipt = _find_receipt(git, impl_dir, topic, tuple(items))
    except CommittedBlocked as exc:
        print(exc, file=sys.stderr)
        print("committed-blocked")
        return 0
    if receipt is not None:
        print("ok" if receipt.settled else "committed-blocked")
        print(*receipt.items, sep="\n")
    elif landed is True:
        print("ok", *items, sep="\n")
    else:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Commit one group.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        Exit code (see the module docstring).
    """
    args = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n", errors="backslashreplace")  # type: ignore[union-attr]
    executable = which("git")
    if executable is None:
        print("resolve_group_replay: git not found on PATH — no commit created")
        return 1
    try:
        if len(args) == 2 and args[0] == "--close-out":
            git = Git(executable, CallBudget.starting_now(_POST_COMMIT_SECONDS))
            impl_dir = Path(args[1]).resolve()
            os.chdir(git.text("rev-parse", "--show-toplevel"))
            return _close_out(git, impl_dir)
        request = _parse(args)
        git = _open_git(executable)
        # recorded paths are repository-relative; plumbing such as update-index reads them against the cwd
        os.chdir(git.text("rev-parse", "--show-toplevel"))
        return commit_group(git, request)
    except subprocess.TimeoutExpired:
        print(f"resolve_group_replay: the {BUDGET_SECONDS} s time budget ran out before the commit — no commit created")
        return 1
    except CommittedBlocked as exc:
        print(f"resolve_group_replay: {exc}")
        return EXIT_COMMITTED_BLOCKED
    except (ReplayError, OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        print(f"resolve_group_replay: {exc}", file=sys.stderr if args[:1] == ["--close-out"] else sys.stdout)
        return 1


if __name__ == "__main__":
    sys.exit(main())
