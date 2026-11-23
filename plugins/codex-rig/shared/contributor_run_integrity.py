#!/usr/bin/env python
"""Detect host-executed state that contributor code left behind in Git metadata or in the user's checkout.

## Purpose

    Codex's permission profiles open ``.git`` under each workspace root for writes, and a review worktree shares the
    common Git directory of the user's checkout. Contributor code that runs inside the sandbox can therefore plant a
    ``core.fsmonitor`` or ``core.hooksPath`` setting, a hook, a forged ``codex-git-approval.json``, a file in an
    ignored interpreter environment, or an auto-loaded file that a later, unsandboxed command or the user's own shell
    would run. ``snapshot`` fingerprints those paths before the first contributor-code execution; ``verify``
    recomputes them after every execution and fails closed on any change.

## Scope

    Fingerprinted, with file modes, for the checkout under review and for ``--main-checkout``:
    - the Git directory's ``config``, ``config.worktree``, ``commondir``, ``HEAD``, ``shallow``,
      ``objects/info/alternates``, ``codex-git-approval.json``, ``hooks/`` and ``info/`` (the symlink targets of hooks
      included), and the ``.git`` pointer file of a linked checkout;
    - every ref in every namespace except ``refs/remotes/`` (a ``git fetch`` of branches is not a change; one that
      brings new tags is, because tags are refs), read by effective value so repacking refs is not a change;
    - everything below ``modules/`` except objects, logs, index and fetch bookkeeping, plus the ``.git`` pointer of
      every submodule checkout;
    - every linked worktree's admin files, and the target of ``core.hooksPath`` and of every included configuration
      file, resolved against the file that names it.
    When the two checkouts have independent Git directories, the same protections cover both. Private review-only
    disposition requires a detached worktree registered in its own Git directory in both directions.
    The user's checkout is also fingerprinted, without trusting the index, by: the bytes of every tracked file that is
    marked assume-unchanged, skip-worktree or fsmonitor-valid; status; the bytes of every modified or untracked file;
    and for ignored files the bytes of executable surfaces (``*.pth``, ``*.py``, ``*.pyc``, native extensions, scripts,
    hooks, ``bin/``, ``pyvenv.cfg``, ``.env``) and the size, mode, inode and change time of the rest. Nested
    repositories and the user's other linked worktrees inside the checkout are walked file by file. Cache directories
    and ``.DS_Store`` count only for their executable files, so tool and IDE cache writes are not changes.
    With ``--watch-surface`` the checkout under review is fingerprinted by ``git diff --binary HEAD``, by the bytes of
    every untracked file and every auto-loaded file, by the index flags above, and by the same ignored-file rules
    (including bytecode caches); use it for review, where the agent never edits that checkout. Without
    ``--main-checkout`` (remediation in the user's own checkout) only the ignored files and index flags of ``--repo``
    are fingerprinted, because the agent edits tracked files on purpose there.
    Not covered: reads, writes outside these paths, the bytes of ignored non-executable files (their change time is
    checked and cannot be reset by an unprivileged process on POSIX; on Windows it is the creation time, so a
    same-size overwrite of such a file there is not detected), a tracked file edited without an index flag in a
    checkout whose status the helper does not read (remediation), and edits to the checkout under review that a
    remediation workflow makes on purpose.

Anchoring:
    The baseline file lives where contributor code can write. ``snapshot`` therefore prints the sha256 of the baseline
    body, the caller keeps that value in its own context, and ``verify --expect`` rejects a file that no longer
    matches it. The resolved Git directories of both checkouts are recorded and compared at verify time, so
    redirecting a ``commondir`` is itself a change. The run directory that holds the baseline, every ``--exclude``
    path and the checkout under review are left out of the user-checkout listings: the workflow writes there on
    purpose and the checkout under review has its own coverage. Every Git call here ignores replace objects, so a
    planted ``refs/replace`` entry cannot change what the helper reads.

## Usage

    contributor_run_integrity.py snapshot --repo PATH [--main-checkout PATH] [--watch-surface] [--exclude PATH]...
        --out FILE
    contributor_run_integrity.py verify --snapshot FILE --expect SHA256
    contributor_run_integrity.py restart-inspected --snapshot FILE --expect SHA256 --inspected-sha256 OBSERVED
        --out NEW_SIBLING
    contributor_run_integrity.py restart-ref-fast-forward --snapshot FILE --expect SHA256 --branch NAME
        --old-oid ORIGINAL --new-oid VERIFIED_UPSTREAM_TIP --out NEW_SIBLING --slot EXCLUSIVE_SLOT --owner RUN_ID

    On eligible ignored-file drift in one checkout, ordinary verification also prints an observed_sha256.
    After inspection, the parent supplies that value to restart-inspected. The helper repeats verification and
    records a new interval only if the same original baseline and inspected fingerprints still match.

    Terminal review cleanup alone uses ``verify-cleanup-retry`` when its parent retained the exact witness OID
    immediately before a previous publication was interrupted. It compares all original fingerprints, accounting
    for that specific direct ref and OID. Its result applies only to retrying cleanup. Contributor execution
    always uses ordinary ``verify``.

## Outputs

    ``snapshot`` writes the baseline file and prints one JSON object with ``ok``, the number of ``recorded``
    fingerprints and the ``sha256`` of the baseline body that the caller keeps. ``verify`` prints one JSON object with
    ``ok`` and the ``changed`` labels, for example ``common:modules`` or ``review:ignored``; an empty list means
    nothing changed. Schema 4 adds ignored_changes on ignored-file drift. Paths are relative to each checkout.
    A tree with over 128 files reports its directory. Review runs under ``.reports/codex/code-review/pr-*/run-*``
    have their own groups. Each checkout keeps up to 128 ordinary groups and 128 review-run groups. Additional
    groups report their common directory. ``omitted`` counts rows beyond the first 64 for each changed label.
    Schema 5 compares an empty environment lock using identity and material metadata where those fields can be
    established independently of ctime. It separately checks Git's generated info/refs against all advertised refs.
    Older snapshots still verify with their original fingerprints and labels.
    On an ordinary verification failure, stderr gives the Git-free disposition for an owned review slot.
    The caller retains the failed interval and performs the slot operation. Verification never changes slot state.

## Failure

    The helper fails closed. A sole authenticated upstream branch fast-forward may start a new interval through
    ``restart-ref-fast-forward`` under the original exclusive claim. Other failures retain their stop or inspected
    restart procedure. No recovery changes a Git ref or accepts the interrupted check.

    Exit codes:
    0 — nothing changed
    1 — a Git command failed, the snapshot file is unreadable, or a fingerprinted path was unreadable at snapshot:
        treat exactly like 3
    2 — bad arguments
    3 — a fingerprinted path changed or became unreadable; the JSON on stdout lists each one

## Used by

    The Contributed Code Execution rule in ``shared/native-skill-contract.md``, which the code-review and
    code-remediate workflows follow around every command that runs contributor code. It is a Codex Rig helper only;
    Claude plugins contain the run with ``untrusted_code_guard.py`` instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

SCHEMA = 5
_MAX_IGNORED_GROUPS = 128
_MAX_IGNORED_FILES_PER_GROUP = 128
_MAX_REPORTED_CHANGES = 64
_IgnoredIndex = dict[str, dict[str, str] | str]
_REVIEW_REPORTS = (".reports", "codex", "code-review")
_REVIEW_REPORT_ROOT = "/".join(_REVIEW_REPORTS)
_FILES = (
    "config",
    "config.worktree",
    "commondir",
    "HEAD",
    "shallow",
    "objects/info/alternates",
    "codex-git-approval.json",
)
_TREES = ("hooks", "info")
_ADMIN = ("commondir", "gitdir", "config.worktree", "HEAD")
_SURFACE_NAMES = frozenset(
    {
        "conftest.py",
        "sitecustomize.py",
        "usercustomize.py",
        "setup.py",
        "noxfile.py",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
        "pyproject.toml",
        "Makefile",
        "package.json",
        "pyvenv.cfg",
        ".env",
    }
)
_EXEC_SUFFIXES = frozenset({".pth", ".py", ".pyc", ".so", ".pyd", ".dll", ".dylib", ".sh", ".cmd", ".bat", ".ps1"})
_EXEC_DIRS = frozenset({"bin", "Scripts", "hooks", ".husky"})
_CACHE_PARTS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".cache", ".idea"}
)
_MODULE_SKIP_DIRS = frozenset({"objects", "logs"})
_MODULE_SKIP_FILES = frozenset({"index", "FETCH_HEAD", "ORIG_HEAD", "gc.log"})
_UNREADABLE = "unreadable"
# Symlink fingerprints prefix the target's fingerprint with the link text, including on nested links.
_UNREADABLE_DIGEST = re.compile(rf"(?:^|:){_UNREADABLE}:(?:[0-9]+|tree)$")
# Integrity measurements read existing objects and must never trigger a partial clone's lazy fetch.
_GIT_ENV = os.environ | {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}
_SLOT_HOLD = (
    "Shared Git or invoking checkout drift was detected, or verification could not establish isolation. "
    "Stop execution, drain all writers and preserve the failed interval and checkout. If this run owns a review slot, "
    "use Git-free review_checkout_slot.py hold with the retained slot and owner."
)


class _RefAdvance(NamedTuple):
    """Parent-retained recovery inputs.

    Branch identity and remote tip require independent upstream evidence.
    """

    branch: str
    old_oid: str
    new_oid: str
    out: str
    slot: Path
    owner: str


class IntegrityError(Exception):
    """A Git command failed or an argument was unusable; ``code`` is the process exit status."""

    def __init__(self, message: str, code: int = 1) -> None:
        """Keep the exit code beside the message."""
        super().__init__(message)
        self.code = code


def _git(repo: str, *args: str, optional: bool = False, reject_warnings: bool = False) -> bytes:
    """Run one read-only Git command in ``repo``; an ``optional`` query that exits non-zero yields empty output."""
    cmd = ["git", "-c", "core.fsmonitor=false", "-C", repo, *args]
    done = subprocess.run(cmd, capture_output=True, check=False, env=_GIT_ENV)  # noqa: S603 - fixed argv, no shell
    if reject_warnings and done.stderr:
        raise IntegrityError("Git could not provide a warning-free ref inventory", 3)
    if done.returncode and not optional:
        raise IntegrityError(f"git {' '.join(args)} failed in {repo}: {done.stderr.decode(errors='replace').strip()}")
    return b"" if done.returncode else done.stdout


def _git_dir(repo: str, flag: str) -> str:
    """Resolve the ``--git-dir`` or ``--git-common-dir`` of ``repo`` to an absolute path."""
    raw = _git(repo, "rev-parse", flag).decode().strip()
    path = Path(raw)
    return str((path if path.is_absolute() else Path(repo) / path).resolve())


def _dirs(repo: str) -> dict[str, str]:
    """Return the resolved common and own Git directories of one checkout."""
    return {"common": _git_dir(repo, "--git-common-dir"), "own": _git_dir(repo, "--git-dir")}


def _file_digest(path: Path) -> str:
    """Hash a file's bytes in chunks together with its permission bits; an unreadable file hashes as unreadable."""
    try:
        sha = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                sha.update(chunk)
        return f"{path.stat().st_mode & 0o7777:o}:{sha.hexdigest()}"
    except OSError as exc:
        return f"{_UNREADABLE}:{exc.errno}"


def _link_digest(path: Path, seen: frozenset[str]) -> str:
    """Hash a symlink by its text and by what it resolves to, so editing the target is a change."""
    text = os.readlink(path)
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return f"symlink:{text}:dangling"
    if str(target) in seen:
        return f"symlink:{text}:loop"
    return f"symlink:{text}:" + _digest(target, seen | {str(target)})


def _digest(path: Path, seen: frozenset[str] = frozenset(), *, exclude: Path | None = None) -> str:
    """Hash a file, or every file below a directory by relative name and content; absent paths hash as ``absent``.

    Symlinks, including symlinked directories met while walking, are followed once per target, so a cycle ends.
    """
    if path.is_symlink():
        return _link_digest(path, seen)
    if path.is_file():
        return _file_digest(path)
    if not path.is_dir():
        return "absent"
    seen |= {str(path.resolve())}
    parts: list[str] = []
    for root, dirs, names in os.walk(path, onerror=lambda _e: parts.append(f"walk:{_UNREADABLE}:0")):
        linked = [d for d in dirs if Path(root, d).is_symlink()]
        parts += [
            f"{Path(root, n).relative_to(path).as_posix()}:{_digest(Path(root, n), seen)}"
            for n in names + linked
            if Path(root, n) != exclude
        ]
    return _seal(sorted(parts))


def _seal(parts: Sequence[str]) -> str:
    """Hash joined fingerprint lines, or report the whole group unreadable when any line could not be read."""
    body = "\n".join(parts)
    return f"{_UNREADABLE}:tree" if f":{_UNREADABLE}:" in body else hashlib.sha256(body.encode()).hexdigest()


def _is_exec_surface(path: Path) -> bool:
    """Tell whether a file is something an interpreter, shell or Git runs, so its bytes are hashed, not its metadata."""
    return path.suffix in _EXEC_SUFFIXES or path.name in _SURFACE_NAMES or path.parent.name in _EXEC_DIRS


def _noise(path: Path, root: Path) -> bool:
    """Tell whether a file is a tool or IDE cache write that is not executable, so its change is not a finding."""
    text, prefix = str(path), f"{root}{os.sep}"
    parts = text[len(prefix) :].split(os.sep) if text.startswith(prefix) else []
    return (path.name == ".DS_Store" or not _CACHE_PARTS.isdisjoint(parts)) and not _is_exec_surface(path)


def _expand(paths: Iterable[Path]) -> list[Path]:
    """Replace each directory entry Git lists (a nested repository) by the files below it, objects excluded."""
    out: list[Path] = []
    for path in paths:
        if path.is_symlink() or not path.is_dir():
            out.append(path)
            continue
        for root, dirs, names in os.walk(path, onerror=lambda e: out.append(Path(str(e.filename)))):
            if Path(root).name == ".git":
                dirs[:] = [d for d in dirs if d != "objects"]
            out += [Path(root, n) for n in names] + [Path(root, d) for d in dirs if Path(root, d).is_symlink()]
    return out


def _lock_stat_output(*argv: str) -> bytes:
    """Read metadata using the platform's fixed system tools in a stable locale and timezone."""
    result = subprocess.run(  # noqa: S603 - callers provide the system executable and fixed options, no shell
        argv, capture_output=True, check=True, env=os.environ | {"TZ": "UTC0", "LC_ALL": "C"}
    )
    return result.stdout


def _empty_lock_identity(path: Path, st: os.stat_result) -> str | None:
    """Recognize an unchanged empty uv lock when its platform exposes material metadata independently of ctime."""
    if not stat.S_ISREG(st.st_mode) or st.st_size or st.st_nlink != 1 or path.parent.is_symlink():
        return None
    # Creation time guards against unlink/recreate with inode reuse. Inspect extended permissions and flags
    # before dropping ctime, which otherwise provides the only evidence for a change to these fields.
    try:
        if sys.platform == "darwin":
            # Darwin Python has no listxattr. ls reports ACLs and attribute names; only the ordinary system
            # provenance attribute is recognized, and its value is included. Other metadata retains ctime.
            listing = _lock_stat_output("/bin/ls", "-ldneq@", str(path))
            extras = listing.splitlines()[1:]
            if extras and not (len(extras) == 1 and re.fullmatch(rb"\s*com\.apple\.provenance\s+\d+\s*", extras[0])):
                return None
            provenance = b""
            if extras:
                provenance = _lock_stat_output("/usr/bin/xattr", "-px", "com.apple.provenance", str(path))
            extended = (st.st_birthtime, st.st_flags, provenance)
        elif sys.platform.startswith("linux") and not os.listxattr(path):
            birth = _lock_stat_output("/usr/bin/stat", "--printf=%w", str(path))
            if birth in (b"", b"-"):
                return None
            flags = _lock_stat_output("/usr/bin/lsattr", "-d", str(path)).split(maxsplit=1)
            if not flags:
                return None
            extended = (birth, flags[0])
        else:
            return None
    except (OSError, subprocess.CalledProcessError):
        return None
    return f"{path}:empty-lock:{st.st_mode:o}:{st.st_dev}:{st.st_ino}:{st.st_uid}:{st.st_gid}:{st.st_mtime_ns}:{extended!r}"


def _ignored_entry(path: Path, lock: Path | None) -> str:
    """Fingerprint an ignored file, using the historical ctime preimage unless a lock has a stronger identity."""
    try:
        st = path.lstat()
    except OSError as exc:
        return f"{path}:{_UNREADABLE}:{exc.errno}"
    if path == lock:
        identity = _empty_lock_identity(path, st)
        if identity is not None:
            return identity
    if path.is_dir() and not path.is_symlink() and not os.access(path, os.R_OK | os.X_OK):
        return f"{path}:{_UNREADABLE}:walk"
    body = _digest(path) if _is_exec_surface(path) and not path.is_dir() else ""
    return f"{path}:{st.st_size}:{st.st_mode:o}:{st.st_ino}:{st.st_ctime_ns}:{body}"


class _IgnoredGroup:
    """Retain file fingerprints up to the diagnostic limit, then keep just their combined fingerprint."""

    def __init__(self) -> None:
        """Start a group with an empty digest and file index."""
        self.digest = hashlib.sha256()
        self.files: dict[str, str] | None = {}

    def add(self, path: Path, root: Path, record: bytes) -> None:
        """Accumulate an entry without reading its content or metadata again."""
        self.digest.update(record)
        self.digest.update(b"\0")
        if self.files is not None:
            if len(self.files) < _MAX_IGNORED_FILES_PER_GROUP:
                self.files[path.relative_to(root).as_posix()] = hashlib.sha256(record).hexdigest()
            else:
                self.files = None


def _ignored_digest(paths: Sequence[Path], root: Path, detail: _IgnoredIndex, schema: int) -> str:
    """Seal every original entry while recording bounded diagnostic fingerprints in the same filesystem pass."""
    lock = root / ".venv" / ".lock" if schema == SCHEMA else None
    depth = len(root.parts)
    aggregate = hashlib.sha256()
    unreadable = False
    groups: dict[str, _IgnoredGroup] = {}
    report_groups: dict[str, _IgnoredGroup] = {}
    for index, path in enumerate(sorted(p for p in paths if not _noise(p, root))):
        entry = _ignored_entry(path, lock)
        data = entry.encode(errors="surrogateescape")
        unreadable |= f":{_UNREADABLE}:" in entry
        # The aggregate reproduces the original newline-joined preimage without retaining every record.
        if index:
            aggregate.update(b"\n")
        aggregate.update(data)

        parts = path.parts
        name, pool, overflow = parts[depth], groups, "."
        if (
            parts[depth : depth + 3] == _REVIEW_REPORTS
            and len(parts) > depth + 5
            and parts[depth + 3].startswith("pr-")
            and parts[depth + 4].startswith("run-")
        ):
            # Independent slots keep large ignored stores from obscuring evidence in individual review runs.
            name, pool, overflow = "/".join(parts[depth : depth + 5]), report_groups, _REVIEW_REPORT_ROOT
        if name not in pool and len(pool) >= _MAX_IGNORED_GROUPS:
            name = overflow
        if name not in pool:
            pool[name] = _IgnoredGroup()
        pool[name].add(path, root, data)
    for name, group in (groups | report_groups).items():
        detail[name] = (
            group.files
            if group.files is not None and name not in {".", _REVIEW_REPORT_ROOT}
            else group.digest.hexdigest()
        )
    return f"{_UNREADABLE}:tree" if unreadable else aggregate.hexdigest()


def _listed(repo: str, *args: str, skip: Sequence[Path] = ()) -> list[Path]:
    """Return the absolute paths ``git ls-files -z`` lists for ``repo``, minus anything under ``skip``."""
    raw = _git(repo, "ls-files", "-z", *args).decode(errors="surrogateescape")
    found = [(Path(repo) / name.rstrip("/")).absolute() for name in raw.split("\0") if name]
    exact = {str(s) for s in skip}
    below = tuple(f"{s}{os.sep}" for s in exact)
    return [p for p in found if str(p) not in exact and not str(p).startswith(below)]


def _modules_digest(modules: Path) -> str:
    """Fingerprint every submodule Git directory, leaving out content-addressed objects, logs and index bookkeeping."""
    if not modules.is_dir():
        return "absent"
    parts: list[str] = []
    for root, dirs, names in os.walk(modules, onerror=lambda _e: parts.append(f"walk:{_UNREADABLE}:0")):
        dirs[:] = [d for d in dirs if d not in _MODULE_SKIP_DIRS]
        linked = [d for d in dirs if Path(root, d).is_symlink()]
        keep = [n for n in names + linked if n not in _MODULE_SKIP_FILES]
        parts += [f"{Path(root, n).relative_to(modules).as_posix()}:{_digest(Path(root, n))}" for n in keep]
    return _seal(sorted(parts))


def _refs(repo: str, published_witness: tuple[str, str] | None = None) -> str:
    """Hash ref values independently of packing; a cleanup retry may project out one exact direct blob ref."""
    if published_witness is None:
        raw = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    else:
        # The trailing space denotes an empty symref. A symbolic alias with the same OID remains visible.
        expected = f"{published_witness[0]} {published_witness[1]} ".encode("ascii")
        listing = _git(repo, "for-each-ref", "--format=%(refname) %(objectname) %(symref)")
        rows = (row.removesuffix(b"\r") for row in listing.split(b"\n"))
        raw = b"\n".join(row.rpartition(b" ")[0] for row in rows if row and row != expected)
    return _ref_digest(raw)


def _ref_digest(raw: bytes) -> str:
    """Use the original snapshot encoding for both the observed and reconstructed ref sets."""
    kept = [line for line in raw.decode(errors="surrogateescape").splitlines() if not line.startswith("refs/remotes/")]
    return hashlib.sha256("\n".join(kept).encode(errors="surrogateescape")).hexdigest()


def _prove_ref_advance(repo: str, advance: _RefAdvance, before: str, after: str) -> None:
    """Reconstruct both aggregate hashes from one listing, changing only the named unoccupied direct branch."""
    old, new = advance.old_oid, advance.new_oid
    if (
        old == new
        or len(old) != len(new)
        or not all(re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", x) for x in (old, new))
    ):
        raise IntegrityError("recovery requires distinct full commit IDs", 3)
    ref = "refs/heads/" + advance.branch
    _git(repo, "check-ref-format", ref)
    ref_bytes = os.fsencode(ref)
    if any(_git(repo, "cat-file", "-t", oid, optional=True).strip() != b"commit" for oid in (old, new)):
        raise IntegrityError("both branch tips must be local commit objects", 3)
    if os.path.lexists(Path(_git_dir(repo, "--git-common-dir")) / "info" / "grafts") or "GIT_GRAFT_FILE" in _GIT_ENV:
        raise IntegrityError("cannot prove ancestry with grafts", 3)
    if _git(repo, "-c", "core.commitGraph=false", "merge-base", old, new, optional=True).strip() != old.encode():
        raise IntegrityError("branch change is not a strict fast-forward", 3)
    # Read branch type, checkout occupancy and both hash inputs from the same inventory. Emit a flag instead of
    # embedding a worktree path, which can contain newlines. Warnings such as a skipped broken ref invalidate proof.
    listing = _git(
        repo,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(symref)%00%(if)%(worktreepath)%(then)occupied%(end)",
        reject_warnings=True,
    )
    current_rows = []
    original_rows = []
    found = False
    for row in listing.splitlines():
        name, oid, symref, occupied = row.split(b"\0")
        current_rows.append(name + b" " + oid)
        if name == ref_bytes:
            if found or symref or occupied or oid != new.encode():
                raise IntegrityError("branch is occupied, symbolic or its tip changed", 3)
            found = True
            oid = old.encode()
        original_rows.append(name + b" " + oid)
    if not found or _ref_digest(b"\n".join(current_rows)) != after:
        raise IntegrityError("ref set changed during recovery or the attested branch is missing", 3)
    if _ref_digest(b"\n".join(original_rows)) != before:
        raise IntegrityError("the proposed old branch tip does not reconstruct the original ref set", 3)


def _advertised_refs(path: Path, repo: str) -> str:
    """Recognize a regular, Git-generated advertisement only when its bytes match every current ref and peeled tag."""
    digest = _digest(path)
    if not os.path.lexists(path) or path.parent.is_symlink() or path.is_symlink():
        return digest
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        return f"nonprivate:{st.st_mode:o}:{st.st_nlink}:{digest}"
    # Windows synthesizes all write permission bits from the read-only attribute.
    if st.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o111) or (os.name != "nt" and st.st_mode & 0o002):
        return digest
    listing = _git(repo, "show-ref", "--dereference", optional=True)
    if listing:
        expected = b"".join(
            line.removesuffix(b"\r").replace(b" ", b"\t", 1) + b"\n" for line in listing.split(b"\n") if line
        )
        mode = f"{stat.S_IMODE(st.st_mode):o}"
        if digest == f"{mode}:{hashlib.sha256(expected).hexdigest()}":
            return f"canonical:{digest}"
    return digest


def _same_advertisement(before: str, after: str) -> bool:
    """Allow an unchanged file or a canonical refresh that keeps the original permissions."""
    original = before.removeprefix("canonical:")
    current = after.removeprefix("canonical:")
    if original == current:
        return True
    # A stale regular file carries a raw mode and digest when refs already moved before the snapshot.
    return after.startswith("canonical:") and (
        before == "absent" or original.partition(":")[0] == current.partition(":")[0]
    )


def _git_metadata(
    common: Path,
    own: Path,
    repo: str,
    prefix: str,
    with_common: bool,
    schema: int,
    published_witness: tuple[str, str] | None = None,
) -> dict[str, str]:
    """Fingerprint one checkout's Git directories: config, hooks, refs, submodules, admin files and pointers."""
    marks: dict[str, str] = {}
    if with_common:
        marks = {f"{prefix}common:{n}": _digest(common / n) for n in _FILES}
        advertised = common / "info" / "refs" if schema == SCHEMA else None
        marks |= {f"{prefix}common:{n}/": _digest(common / n, exclude=advertised) for n in _TREES}
        if advertised is not None:
            marks[f"{prefix}common:info/refs"] = _advertised_refs(advertised, repo)
        marks[f"{prefix}common:refs"] = _refs(repo, published_witness)
        marks[f"{prefix}common:modules"] = _modules_digest(common / "modules")
        for admin in sorted(common.glob("worktrees/*")):
            marks |= {f"{prefix}admin:{admin.name}/{n}": _digest(admin / n) for n in _ADMIN}
    if own != common:
        marks[f"{prefix}worktree:hooks/"] = _digest(own / "hooks")
        marks[f"{prefix}worktree:config.worktree"] = _digest(own / "config.worktree")
        marks[f"{prefix}worktree:commondir"] = _digest(own / "commondir")
    pointer = Path(repo) / ".git"
    if pointer.is_file():
        marks[f"{prefix}worktree:.git-pointer"] = _digest(pointer)
    return marks


def _config_targets(label: str, repo: str) -> dict[str, str]:
    """Fingerprint the hooks directory and every included file for one checkout, each resolved where Git resolves it."""
    marks: dict[str, str] = {}
    hooks = _git(repo, "config", "--get", "core.hooksPath", optional=True).decode().strip()
    if hooks:
        marks[f"config:{label}:hooksPath"] = _digest((Path(repo) / os.path.expanduser(hooks)).resolve())
    listed = _git(
        repo, "config", "--show-origin", "--get-regexp", r"^(include|includeif)\..*path$", optional=True
    ).decode(errors="replace")
    for line in listed.splitlines():
        origin, _tab, rest = line.partition("\t")
        value = rest.partition(" ")[2]
        if origin.startswith("file:") and value:
            base = (Path(repo) / origin[5:]).parent
            key = f"config:{label}:include:{value}"
            seen = marks.get(key, "") + _digest((base / os.path.expanduser(value)).resolve())
            marks[key] = hashlib.sha256(seen.encode()).hexdigest()
    return marks


def _flagged_entries(repo: str) -> Iterable[tuple[str, str]]:
    """List index entries whose flags can hide edits to the file from Git status and diff."""
    raw = _git(repo, "ls-files", "-z", "-v", "-f").decode(errors="surrogateescape")
    for entry in raw.split("\0"):
        tag, _space, name = entry.partition(" ")
        if entry and tag != "H":
            yield tag, name


def _flagged(repo: str) -> str:
    """Hash each flag alongside the file's own bytes because Git can report the entry clean after an edit."""
    return _seal(sorted(f"{tag} {name}:{_digest(Path(repo) / name)}" for tag, name in _flagged_entries(repo)))


def _submodule_pointers(repo: str) -> str:
    """Hash the ``.git`` pointer file of every submodule checkout, which names the Git directory Git will read."""
    raw = _git(repo, "ls-files", "-s", "-z").decode(errors="surrogateescape")
    names = [entry.partition("\t")[2] for entry in raw.split("\0") if entry.startswith("160000")]
    return _seal([f"{name}:{_digest(Path(repo) / name / '.git')}" for name in sorted(names)])


def _status(repo: str, skip: Sequence[Path]) -> str:
    """Hash ``git status`` with the run directory and other excluded paths left out of its untracked listing."""
    excluded = []
    for path in skip:
        try:
            excluded.append(f":(exclude){path.relative_to(repo).as_posix()}")
        except ValueError:
            continue
    excluded += [":(exclude,glob)**/.DS_Store", *(f":(exclude,glob)**/{part}/**" for part in sorted(_CACHE_PARTS))]
    tail = ["--", ".", *excluded]
    return hashlib.sha256(_git(repo, "status", "--porcelain=v1", "-z", *tail)).hexdigest()


def _checkout_content(
    repo: str, skip: Sequence[Path], ignored_details: dict[str, _IgnoredIndex], schema: int, *, edits_expected: bool
) -> dict[str, str]:
    """Fingerprint the user's checkout; with ``edits_expected`` the tracked and untracked content is left out."""
    root = Path(repo)
    marks = {
        "main-checkout:ignored": _ignored_digest(
            _expand(_listed(repo, "-o", "-i", "--exclude-standard", skip=skip)),
            root,
            ignored_details.setdefault("main-checkout:ignored", {}),
            schema,
        ),
        "main-checkout:index-flags": _flagged(repo),
        "main-checkout:submodule-pointers": _submodule_pointers(repo),
    }
    if edits_expected:
        return marks
    own = _expand(_listed(repo, "-m", "-o", "--exclude-standard", skip=skip))
    files = [p for p in own if (p.is_file() or p.is_symlink()) and not _noise(p, root)]
    marks["main-checkout:status"] = _status(repo, skip)
    marks["main-checkout:dirty-content"] = _seal([f"{p}:{_digest(p)}" for p in sorted(files)])
    return marks


def _review_content(repo: str, ignored_details: dict[str, _IgnoredIndex], schema: int) -> dict[str, str]:
    """Fingerprint the checkout under review: tracked edits, untracked and ignored files, auto-loaded files."""
    root = Path(repo)
    diff = _git(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", optional=True)
    cached = _listed(repo, "-c")
    fresh = _expand(_listed(repo, "-o", "--exclude-standard"))
    ignored = _expand(_listed(repo, "-o", "-i", "--exclude-standard"))
    picked = [p for p in cached + fresh + ignored if p.name in _SURFACE_NAMES or p.suffix == ".pth"]
    marks = {
        "review:diff": hashlib.sha256(diff).hexdigest(),
        "review:index-flags": _flagged(repo),
        "review:untracked": _seal([f"{p}:{_digest(p)}" for p in sorted(fresh) if not _noise(p, root)]),
        "review:ignored": _ignored_digest(ignored, root, ignored_details.setdefault("review:ignored", {}), schema),
    }
    return marks | {f"surface:{p.relative_to(repo).as_posix()}": _digest(p) for p in sorted(set(picked))}


def fingerprint(
    spec: dict[str, object],
    ignored_details: dict[str, _IgnoredIndex],
    *,
    published_witness: tuple[str, str] | None = None,
) -> dict[str, str]:
    """Return the digest of every fingerprinted path for one recorded spec, keyed by a stable label."""
    repo = str(spec["repo"])
    main = spec["main_checkout"]
    schema = spec.get("schema")
    dirs = dict(spec["dirs"])  # type: ignore[arg-type]
    skip = [Path(p) for p in spec["exclude"]]  # type: ignore[union-attr]
    marks = _git_metadata(
        Path(dirs["repo"]["common"]), Path(dirs["repo"]["own"]), repo, "", True, schema, published_witness
    )
    marks |= _config_targets("repo", repo)
    if main:
        mdirs = dirs["main"]
        marks |= _git_metadata(
            Path(mdirs["common"]),
            Path(mdirs["own"]),
            str(main),
            "main:",
            mdirs["common"] != dirs["repo"]["common"],
            schema,
        )
        marks |= _config_targets("main", str(main)) | _checkout_content(
            str(main), skip, ignored_details, schema, edits_expected=False
        )
    else:
        marks |= _checkout_content(repo, skip, ignored_details, schema, edits_expected=True)
    if spec["watch_surface"]:
        marks |= _review_content(repo, ignored_details, schema)
    return marks


def _canonical(body: dict[str, object]) -> str:
    """Return the sha256 of the baseline body in its one canonical serialization."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _spec(
    repo: str, main_checkout: str | None, watch_surface: bool, out: str, exclude: Sequence[str]
) -> dict[str, object]:
    """Build the recorded description of what is fingerprinted and where."""
    main = str(Path(main_checkout).resolve()) if main_checkout else None
    skip = {str(Path(p).resolve()) for p in (*exclude, str(Path(out).resolve().parent), repo)}
    anchor = Path(main or repo)
    skip = {p for p in skip if Path(p) != anchor and Path(p) not in anchor.parents}
    return {
        "schema": SCHEMA,
        "repo": repo,
        "main_checkout": main,
        "dirs": {"repo": _dirs(repo), "main": _dirs(main) if main else None},
        "watch_surface": watch_surface,
        "exclude": sorted(skip),
    }


def snapshot(repo: str, main_checkout: str | None, watch_surface: bool, out: str, exclude: Sequence[str]) -> int:
    """Record the fingerprints to ``out`` and print their count with the sha256 the caller must keep."""
    spec = _spec(repo, main_checkout, watch_surface, out, exclude)
    ignored_details: dict[str, _IgnoredIndex] = {}
    marks = fingerprint(spec, ignored_details)
    bad = sorted(label for label, value in marks.items() if _UNREADABLE_DIGEST.search(value))
    if bad:
        raise IntegrityError(f"unreadable at snapshot, nothing was recorded: {', '.join(bad)}")
    body = spec | {"marks": marks, "ignored_details": ignored_details}
    Path(out).write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"ok": True, "recorded": len(marks), "sha256": _canonical(body)}))
    return 0


def _load(snapshot_file: str, expect: str) -> dict[str, object]:
    """Read the baseline and require it to match the digest the caller kept from ``snapshot``."""
    try:
        body = json.loads(Path(snapshot_file).read_text(encoding="utf-8"))
        marks = dict(body["marks"])
        keys = ("repo", "main_checkout", "dirs", "watch_surface", "exclude")
        spec = {key: body[key] for key in keys}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise IntegrityError(f"snapshot unreadable: {exc}") from exc
    if _canonical(body) != expect:
        print(json.dumps({"ok": False, "changed": ["snapshot-file"]}))
        raise IntegrityError("snapshot file does not match the digest kept from snapshot", 3)
    return spec | {"schema": body.get("schema"), "marks": marks, "ignored_details": body.get("ignored_details", {})}


def _redirected(body: dict[str, object]) -> list[str]:
    """Return a label for every checkout whose resolved Git directories differ from the recorded ones."""
    labels: list[str] = []
    recorded: dict[str, dict[str, str] | None] = body["dirs"]  # type: ignore[assignment]
    for key, path in (("repo", body["repo"]), ("main", body["main_checkout"])):
        if recorded[key] and _dirs(str(path)) != recorded[key]:
            labels.append(f"git-dir-redirect:{key}")
    return labels


def _changed_ignored_paths(before: _IgnoredIndex, after: _IgnoredIndex) -> Iterable[tuple[str, str]]:
    """Compare captured groups without attributing additions or removals across a diagnostic limit."""
    for group in sorted(before.keys() | after.keys()):
        old = before.get(group, {})
        new = after.get(group, {})
        if old == new:
            continue
        overflow = _REVIEW_REPORT_ROOT if group.startswith(_REVIEW_REPORT_ROOT + "/") else "."
        if group not in {".", _REVIEW_REPORT_ROOT} and (
            (group not in before and overflow in before) or (group not in after and overflow in after)
        ):
            # The other snapshot may have represented this group only in its overflow digest.
            continue
        if isinstance(old, str) or isinstance(new, str):
            yield "directories", group
            continue
        for path in sorted(old.keys() | new.keys()):
            if path not in old:
                yield "added", path
            elif path not in new:
                yield "removed", path
            elif old[path] != new[path]:
                yield "modified", path


def _ignored_changes(before: _IgnoredIndex, after: _IgnoredIndex) -> dict[str, object]:
    """Limit displayed path evidence while retaining the count of further changed files or directories."""
    changes: dict[str, list[str]] = {}
    count = 0
    for count, (operation, path) in enumerate(_changed_ignored_paths(before, after), 1):
        if count <= _MAX_REPORTED_CHANGES:
            changes.setdefault(operation, []).append(path)
    result: dict[str, object] = dict(changes)
    if count > _MAX_REPORTED_CHANGES:
        result["omitted"] = count - _MAX_REPORTED_CHANGES
    return result


def verify(snapshot_file: str, expect: str) -> int:
    """Recompute the recorded fingerprints; return 3 with the changed labels when any differ or became unreadable."""
    return _verify_snapshot(snapshot_file, expect, None)


def _detached_review(body: dict[str, object]) -> bool:
    """Require a distinct, detached review with matching forward and backward registration in its own Git common dir."""
    if not body["watch_surface"] or not body["main_checkout"] or body["repo"] == body["main_checkout"]:
        return False
    dirs = dict(body["dirs"])  # type: ignore[arg-type]
    common, own = Path(dirs["repo"]["common"]), Path(dirs["repo"]["own"])
    if own == Path(dirs["main"]["own"]) or own.parent != common / "worktrees":
        return False
    repo = Path(str(body["repo"]))
    pointer, backlink, head_file = repo / ".git", own / "gitdir", own / "HEAD"
    try:
        # Both pointers are fingerprinted. Require the pair to identify this checkout, including for Git's
        # relative-worktree-links format, before classifying its files as private to the recorded worktree.
        for path in (pointer, backlink, head_file):
            if not path.is_file() or path.resolve(strict=True) != path:
                return False
        prefix, separator, dest = os.fsdecode(pointer.read_bytes().rstrip(b"\r\n")).partition("gitdir: ")
        back = os.fsdecode(backlink.read_bytes().rstrip(b"\r\n"))
        if (
            prefix
            or not separator
            or (repo / dest).resolve(strict=True) != own
            or (own / back).resolve(strict=True) != pointer
        ):
            return False
        head = head_file.read_bytes()
    except (OSError, RuntimeError, ValueError):
        return False
    return re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\r?\n?", head) is not None


def _review_files_confined(
    repo: str,
    changed: list[str],
    before: dict[str, str],
    prior_ignored: _IgnoredIndex | None,
    current_ignored: _IgnoredIndex,
) -> bool:
    """Establish that the inputs to each changed review fingerprint are ordinary files in this checkout."""
    root = Path(repo)
    paths: set[Path] = set()
    empty = hashlib.sha256(b"").hexdigest()
    for label in changed:
        if label.startswith("surface:"):
            if before.get(label, "").startswith("symlink:"):
                return False
            paths.add(root / label.removeprefix("surface:"))
        elif label != "review:ignored" and before[label] != empty:
            # The old aggregate cannot identify a removed or replaced external link.
            return False
    if "review:diff" in changed:
        names = _git(repo, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", "HEAD")
        paths.update(root / name for name in names.decode(errors="surrogateescape").split("\0") if name)
    if "review:index-flags" in changed:
        paths.update(root / name for _, name in _flagged_entries(repo))
    if "review:untracked" in changed:
        paths.update(_expand(_listed(repo, "-o", "--exclude-standard")))
    if "review:ignored" in changed:
        # An empty retained aggregate proves every current path was added, even on an older snapshot layout.
        if prior_ignored is None and before["review:ignored"] != empty:
            return False
        rows = list(_changed_ignored_paths(prior_ignored or {}, current_ignored))
        if not rows:
            return False
        fresh_trees: set[Path] = set()
        for operation, name in rows:
            if operation == "added":
                paths.add(root / name)
            elif operation == "directories" and name not in (prior_ignored or {}):
                # A group absent from the old bounded index might still have been folded into its overflow.
                overflow = _REVIEW_REPORT_ROOT if name.startswith(_REVIEW_REPORT_ROOT + "/") else "."
                if overflow in (prior_ignored or {}) or (name in {".", _REVIEW_REPORT_ROOT} and prior_ignored):
                    return False
                fresh_trees.add(root / name)
            else:
                return False
        if fresh_trees:
            candidates = _expand(_listed(repo, "-o", "-i", "--exclude-standard"))
            paths.update(
                p for p in candidates if not _noise(p, root) and any(p.is_relative_to(tree) for tree in fresh_trees)
            )
    try:
        # A leaf or ancestor link can redirect reads. A multiply linked inode can belong to another checkout.
        return bool(paths) and all(
            path.resolve(strict=True) == path
            and path.is_relative_to(root)
            and path.is_file()
            and path.stat().st_nlink == 1
            for path in paths
        )
    except (OSError, RuntimeError):
        return False


def _write_successor(snapshot_file: str, out: str, successor: dict[str, object]) -> None:
    """Create a fresh sibling, retaining the original baseline and every prior recovery attempt."""
    original, destination = Path(snapshot_file), Path(out)
    if original.is_symlink() or destination.parent.resolve() != original.parent.resolve():
        raise IntegrityError("restart requires a new sibling of the original snapshot", 2)
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(successor, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        raise IntegrityError(f"could not create restart snapshot: {exc}", 2) from exc


def _changed_labels(
    body: dict[str, object], before: dict[str, str], after: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Report all changed and unreadable fingerprints using the snapshot's original comparison rules."""
    drifted = [
        label
        for label in before.keys() | after.keys()
        if before.get(label) != after.get(label)
        and not (
            label in {"common:info/refs", "main:common:info/refs"}
            and label in before
            and label in after
            and _same_advertisement(before[label], after[label])
        )
    ]
    unreadable = [label for label, value in after.items() if _UNREADABLE_DIGEST.search(value)]
    changed = sorted(set(_redirected(body)) | set(drifted) | set(unreadable))
    return changed, unreadable


def _restart_ref_fast_forward(snapshot_file: str, expect: str, advance: _RefAdvance) -> int:
    """Seal the next interval under the same exclusive claim only after independently checking the old interval."""
    sys.pycache_prefix = str(Path(__file__).resolve())
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from review_checkout_slot import recovery_claim

    body = _load(snapshot_file, expect)
    dirs = dict(body["dirs"])  # type: ignore[arg-type]
    common = Path(dirs["repo"]["common"])
    if body["schema"] not in (3, 4, SCHEMA) or (dirs["main"] and Path(dirs["main"]["common"]) != common):
        raise IntegrityError("recovery requires a supported baseline in the same repository", 3)
    try:
        with recovery_claim(common, advance.slot, advance.owner):
            details: dict[str, _IgnoredIndex] = {}
            before, after = dict(body["marks"]), fingerprint(body, details)  # type: ignore[arg-type]
            changed, unreadable = _changed_labels(body, before, after)
            if changed != ["common:refs"] or unreadable:
                raise IntegrityError(f"ref recovery requires sole shared ref drift; changed={changed}", 3)
            _prove_ref_advance(str(body["repo"]), advance, before["common:refs"], after["common:refs"])
            successor = body | {"marks": after, "ignored_details": details}
            _write_successor(snapshot_file, advance.out, successor)
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"could not recover exclusive claim: {exc}", 3) from exc
    print(
        json.dumps(
            {
                "ok": True,
                "status": "restarted",
                "sha256": _canonical(successor),
                "recorded": len(after),
                "branch": advance.branch,
                "old_oid": advance.old_oid,
                "new_oid": advance.new_oid,
                "slot": str(advance.slot),
                "owner": advance.owner,
            }
        )
    )
    return 0


def _verify_snapshot(
    snapshot_file: str,
    expect: str,
    published_witness: tuple[str, str] | None,
    *,
    restart: tuple[str, str] | None = None,
) -> int:
    """Compare against the original external digest, retaining all ordinary checks during cleanup recovery."""
    body = _load(snapshot_file, expect)
    details: dict[str, _IgnoredIndex] = {}
    before, after = dict(body["marks"]), fingerprint(body, details, published_witness=published_witness)  # type: ignore[arg-type]
    changed, unreadable = _changed_labels(body, before, after)
    result: dict[str, object] = {"ok": not changed, "changed": changed}
    if published_witness is not None:
        result["scope"] = "terminal-cleanup-retry"
    prior = body["ignored_details"]
    diagnostics = {
        label: _ignored_changes(prior[label], details[label])
        for label in changed
        if label in prior and label in details
    }
    if diagnostics:
        result["ignored_changes"] = diagnostics
    review_marks = {"review:diff", "review:index-flags", "review:untracked", "review:ignored"}
    detached_review_drift = (
        published_witness is None
        and bool(changed)
        and not unreadable
        and body["schema"] in (3, 4, SCHEMA)
        and review_marks <= before.keys() & after.keys()
        and all(label in review_marks or label.startswith("surface:") for label in changed)
        and _detached_review(body)
    )
    if (
        published_witness is None
        and body["schema"] in (3, 4, SCHEMA)
        and (changed == ["main-checkout:ignored"] or (detached_review_drift and changed == ["review:ignored"]))
        and before.keys() == after.keys()
        and not unreadable
    ):
        # Preserve the algorithm of an older interval (and any digest already inspected by its parent).
        successor = body | {
            "schema": 4 if body["schema"] == 3 else body["schema"],
            "marks": after,
            "ignored_details": details,
        }
        digest = _canonical(successor)
        observed = _canonical({"original_sha256": expect, "successor_sha256": digest})
        result["observed_sha256"] = observed
        dirs = dict(body["dirs"])  # type: ignore[arg-type]
        slot_dirs = dirs["repo"] if _detached_review(body) else dirs["main"] or dirs["repo"]
        result["git_common_dir"] = slot_dirs["common"]
        if restart is not None:
            inspected, out = restart
            if inspected != observed:
                raise IntegrityError("current fingerprints differ from the inspected observation", 3)
            _write_successor(snapshot_file, out, successor)
            print(json.dumps({"ok": True, "recorded": len(after), "sha256": digest}))
            return 0
    if restart is not None:
        print(json.dumps(result | {"ok": False}))
        raise IntegrityError("restart requires an eligible, readable ignored-file change in exactly one checkout", 3)
    if changed and published_witness is None:
        message = _SLOT_HOLD
        # Ordinary release needs evidence of confinement. A held slot can still resume after explicit inspection.
        if detached_review_drift and _review_files_confined(
            str(body["repo"]), changed, before, prior.get("review:ignored"), details["review:ignored"]
        ):
            message = (
                "Verification detected changes only in the isolated review checkout. Stop execution and preserve "
                "the checkout and failed interval. Once all writers have stopped and verification covers their last "
                "writes, use Git-free review_checkout_slot.py release with the retained slot and owner if this run "
                "owns a slot. Later unverified writes require holding the slot for inspection."
            )
            if "observed_sha256" in result:
                message += " Resuming this checkout requires user inspection and restart-inspected."
        elif "observed_sha256" in result:
            message += (
                " Before an inspected release or restart-inspected, obtain user inspection of the changed paths, "
                "shared Git state, the invoking checkout and any affected external targets."
            )
        print(message, file=sys.stderr)
    print(json.dumps(result))
    return 3 if changed else 0


def _parser() -> argparse.ArgumentParser:
    """Build the snapshot, ordinary verify and terminal-cleanup recovery CLI."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--repo", required=True)
    snap.add_argument("--main-checkout")
    snap.add_argument("--watch-surface", action="store_true")
    snap.add_argument("--exclude", action="append", default=[])
    snap.add_argument("--out", required=True)
    check = sub.add_parser("verify")
    retry = sub.add_parser(
        "verify-cleanup-retry", help="Verify a terminal cleanup retry with its parent-retained witness OID."
    )
    retry.add_argument("--retry-witness", required=True)
    restart = sub.add_parser("restart-inspected", help="Start a new interval from the exact inspected observation.")
    restart.add_argument("--inspected-sha256", required=True)
    restart.add_argument("--out", required=True)
    advance = sub.add_parser("restart-ref-fast-forward", help="Recover a verified upstream branch fast-forward.")
    for option in ("branch", "old-oid", "new-oid", "out", "owner"):
        advance.add_argument("--" + option, required=True)
    advance.add_argument("--slot", type=Path, required=True)
    for command in (check, retry, restart, advance):
        command.add_argument("--snapshot", required=True)
        command.add_argument("--expect", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch the requested integrity check and map a failure to its exit status."""
    args = _parser().parse_args(argv)
    try:
        if args.mode == "snapshot":
            repo = str(Path(args.repo).resolve())
            return snapshot(repo, args.main_checkout, args.watch_surface, args.out, args.exclude)
        if args.mode == "restart-ref-fast-forward":
            advance = _RefAdvance(args.branch, args.old_oid, args.new_oid, args.out, args.slot, args.owner)
            return _restart_ref_fast_forward(args.snapshot, args.expect, advance)
        if args.mode == "restart-inspected":
            return _verify_snapshot(args.snapshot, args.expect, None, restart=(args.inspected_sha256, args.out))
        if args.mode == "verify-cleanup-retry":
            # Only the review cleanup caller uses this route. Ordinary contributor execution always calls verify.
            sys.pycache_prefix = str(Path(__file__).resolve())
            from review_worktree_replay import witness_ref

            snapshot_file = Path(args.snapshot)
            run = snapshot_file.parent
            if (
                not re.fullmatch(r"integrity(?:-restart(?:-[1-9][0-9]*)?)?\.json", snapshot_file.name)
                or not re.fullmatch(r"run-[0-9]{3,}", run.name)
                or not re.fullmatch(r"pr-[1-9][0-9]*", run.parent.name)
                or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", args.retry_witness)
            ):
                raise IntegrityError("invalid cleanup retry identity", 2)
            return _verify_snapshot(args.snapshot, args.expect, (witness_ref(run), args.retry_witness))
        return verify(args.snapshot, args.expect)
    except IntegrityError as exc:
        print(f"contributor-run-integrity: {exc}", file=sys.stderr)
        if args.mode == "verify":
            print(_SLOT_HOLD, file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
