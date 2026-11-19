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
    Older aggregate-only snapshots still verify and report their original labels.

## Failure

    The helper fails closed. Every failure is handled like a detected change: the caller stops running contributor
    code and reports the labels, restores nothing automatically, and asks the user before it continues.

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
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

SCHEMA = 4
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
_GIT_ENV = os.environ | {"GIT_NO_REPLACE_OBJECTS": "1"}


class IntegrityError(Exception):
    """A Git command failed or an argument was unusable; ``code`` is the process exit status."""

    def __init__(self, message: str, code: int = 1) -> None:
        """Keep the exit code beside the message."""
        super().__init__(message)
        self.code = code


def _git(repo: str, *args: str, optional: bool = False) -> bytes:
    """Run one read-only Git command in ``repo``; an ``optional`` query that exits non-zero yields empty output."""
    cmd = ["git", "-c", "core.fsmonitor=false", "-C", repo, *args]
    done = subprocess.run(cmd, capture_output=True, check=False, env=_GIT_ENV)  # noqa: S603 - fixed argv, no shell
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


def _digest(path: Path, seen: frozenset[str] = frozenset()) -> str:
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
            f"{Path(root, n).relative_to(path).as_posix()}:{_digest(Path(root, n), seen)}" for n in names + linked
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


def _ignored_entry(path: Path) -> str:
    """Fingerprint one ignored file with the same metadata and executable-content fields as schema 3."""
    try:
        st = path.lstat()
    except OSError as exc:
        return f"{path}:{_UNREADABLE}:{exc.errno}"
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


def _ignored_digest(paths: Sequence[Path], root: Path, detail: _IgnoredIndex) -> str:
    """Seal every original entry while recording bounded diagnostic fingerprints in the same filesystem pass."""
    depth = len(root.parts)
    aggregate = hashlib.sha256()
    unreadable = False
    groups: dict[str, _IgnoredGroup] = {}
    report_groups: dict[str, _IgnoredGroup] = {}
    for index, path in enumerate(sorted(p for p in paths if not _noise(p, root))):
        entry = _ignored_entry(path)
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
    kept = [line for line in raw.decode(errors="surrogateescape").splitlines() if not line.startswith("refs/remotes/")]
    return hashlib.sha256("\n".join(kept).encode(errors="surrogateescape")).hexdigest()


def _git_metadata(
    common: Path, own: Path, repo: str, prefix: str, with_common: bool, published_witness: tuple[str, str] | None = None
) -> dict[str, str]:
    """Fingerprint one checkout's Git directories: config, hooks, refs, submodules, admin files and pointers."""
    marks: dict[str, str] = {}
    if with_common:
        marks = {f"{prefix}common:{n}": _digest(common / n) for n in _FILES}
        marks |= {f"{prefix}common:{n}/": _digest(common / n) for n in _TREES}
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


def _flagged(repo: str) -> str:
    """Hash the bytes of every tracked file whose index entry hides it from status and diff.

    Assume-unchanged, skip-worktree and fsmonitor-valid files are reported clean by Git however they were edited, so
    the index is not trusted for them: the flag and the file's own bytes are fingerprinted instead.
    """
    raw = _git(repo, "ls-files", "-z", "-v", "-f").decode(errors="surrogateescape")
    parts = []
    for entry in raw.split("\0"):
        tag, _space, name = entry.partition(" ")
        if entry and tag != "H":
            parts.append(f"{tag} {name}:{_digest(Path(repo) / name)}")
    return _seal(sorted(parts))


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
    repo: str, skip: Sequence[Path], ignored_details: dict[str, _IgnoredIndex], *, edits_expected: bool
) -> dict[str, str]:
    """Fingerprint the user's checkout; with ``edits_expected`` the tracked and untracked content is left out."""
    root = Path(repo)
    marks = {
        "main-checkout:ignored": _ignored_digest(
            _expand(_listed(repo, "-o", "-i", "--exclude-standard", skip=skip)),
            root,
            ignored_details.setdefault("main-checkout:ignored", {}),
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


def _review_content(repo: str, ignored_details: dict[str, _IgnoredIndex]) -> dict[str, str]:
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
        "review:ignored": _ignored_digest(ignored, root, ignored_details.setdefault("review:ignored", {})),
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
    dirs = dict(spec["dirs"])  # type: ignore[arg-type]
    skip = [Path(p) for p in spec["exclude"]]  # type: ignore[union-attr]
    marks = _git_metadata(Path(dirs["repo"]["common"]), Path(dirs["repo"]["own"]), repo, "", True, published_witness)
    marks |= _config_targets("repo", repo)
    if main:
        mdirs = dirs["main"]
        marks |= _git_metadata(
            Path(mdirs["common"]), Path(mdirs["own"]), str(main), "main:", mdirs["common"] != dirs["repo"]["common"]
        )
        marks |= _config_targets("main", str(main)) | _checkout_content(
            str(main), skip, ignored_details, edits_expected=False
        )
    else:
        marks |= _checkout_content(repo, skip, ignored_details, edits_expected=True)
    if spec["watch_surface"]:
        marks |= _review_content(repo, ignored_details)
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
    bad = sorted(label for label, value in marks.items() if _UNREADABLE in value.split(":", 2)[:2])
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
    return spec | {"marks": marks, "ignored_details": body.get("ignored_details", {})}


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


def _verify_snapshot(snapshot_file: str, expect: str, published_witness: tuple[str, str] | None) -> int:
    """Compare against the original external digest, retaining all ordinary checks during cleanup recovery."""
    body = _load(snapshot_file, expect)
    details: dict[str, _IgnoredIndex] = {}
    before, after = dict(body["marks"]), fingerprint(body, details, published_witness=published_witness)  # type: ignore[arg-type]
    drifted = [label for label in before.keys() | after.keys() if before.get(label) != after.get(label)]
    unreadable = [label for label, value in after.items() if _UNREADABLE in value.split(":", 2)[:2]]
    changed = sorted(set(_redirected(body)) | set(drifted) | set(unreadable))
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
    for command in (check, retry):
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
        if args.mode == "verify-cleanup-retry":
            # Only the review cleanup caller uses this route. Ordinary contributor execution always calls verify.
            sys.pycache_prefix = str(Path(__file__).resolve())
            from review_worktree_replay import witness_ref

            snapshot_file = Path(args.snapshot)
            run = snapshot_file.parent
            if (
                snapshot_file.name != "integrity.json"
                or not re.fullmatch(r"run-[0-9]{3,}", run.name)
                or not re.fullmatch(r"pr-[1-9][0-9]*", run.parent.name)
                or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", args.retry_witness)
            ):
                raise IntegrityError("invalid cleanup retry identity", 2)
            return _verify_snapshot(args.snapshot, args.expect, (witness_ref(run), args.retry_witness))
        return verify(args.snapshot, args.expect)
    except IntegrityError as exc:
        print(f"contributor-run-integrity: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
