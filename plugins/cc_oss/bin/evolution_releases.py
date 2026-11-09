"""Release index, repository acquisition and open-bug series for ``/oss:analyse evolution``.

Builds the per-line release chronology that every later evolution stage reads, decides which repository a scan
targets, keeps the dedicated full clone current, and computes the approximate open-bug series from GitHub.

Usage:
    python evolution_releases.py index --repo DIR --out FILE
    python evolution_releases.py target [--target OWNER/REPO|URL]

Outputs:
    ``index`` writes the release index JSON (shape documented on :func:`build_release_index`) to ``--out`` and prints
    a one-line summary. ``target`` prints the resolved target (shape documented on :func:`resolve_target`) as one line
    of JSON.

Importable API, called directly by the orchestrator: :func:`resolve_target`, :func:`sync_clone`, :func:`is_shallow`,
:func:`build_release_index` and :func:`fetch_open_bug_series`.

Exit codes:
    0: success.
    1: git, GitHub or input error (message on stderr).
    2: usage error (argparse).
    3: blocked: the repository is a shallow clone, or no tag parses as a release version.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _evolution_common import (
    ALGO_VERSIONS,
    SCHEMA_VERSION,
    EvolutionError,
    GitError,
    Version,
    atomic_write_json,
    fingerprint,
    parse_remote_url,
    parse_version,
    run_git,
    sha256_text,
)

#: Name of the release line that runs through the highest version (the development trunk).
TRUNK_LINE = "main"

#: Seconds allowed for one clone or fetch of the dedicated clone; a large repository takes minutes.
NETWORK_TIMEOUT_S = 1800

#: Exit code for a blocked run (shallow clone or no release tag).
EXIT_BLOCKED = 3

#: Seconds allowed for the single history walk behind the index.
_WALK_TIMEOUT_S = 600

#: Seconds allowed for one ``gh`` call.
_GH_TIMEOUT_S = 120

_TAG_PREFIX = "refs/tags/"

#: One ``for-each-ref`` line per tag: ref, object type and SHA, peeled type and SHA, creation time, peeled commit time.
#: ``creatordate`` is the tagger date of an annotated tag and the committer date of a lightweight one.
_TAG_FORMAT = "%09".join(
    (
        "%(refname)",
        "%(objecttype)",
        "%(objectname)",
        "%(*objecttype)",
        "%(*objectname)",
        "%(creatordate:unix)",
        "%(*committerdate:unix)",
    )
)

#: ``OWNER/REPO`` shorthand for a GitHub repository (one slash, no scheme, no user part).
_SHORTHAND = re.compile(r"^[^/\s:@]+/[^/\s:@]+$")

#: Timeline event type to the issue state it leaves behind (True = open).
_EVENT_STATE = {"ClosedEvent": False, "ReopenedEvent": True}

_EVENT_FIELDS = (
    "pageInfo { hasNextPage endCursor }"
    " nodes { __typename ... on ClosedEvent { createdAt } ... on ReopenedEvent { createdAt } }"
)
_LABELS_QUERY = (
    "query($owner: String!, $name: String!, $cursor: String) { repository(owner: $owner, name: $name) {"
    " labels(first: 100, after: $cursor) { pageInfo { hasNextPage endCursor } nodes { name } } } }"
)
_ISSUES_QUERY = (
    "query($owner: String!, $name: String!, $label: String!, $cursor: String) {"
    " repository(owner: $owner, name: $name) {"
    " issues(first: 100, after: $cursor, labels: [$label], orderBy: {field: CREATED_AT, direction: ASC}) {"
    " pageInfo { hasNextPage endCursor } nodes { number createdAt closedAt"
    " timelineItems(first: 100, itemTypes: [CLOSED_EVENT, REOPENED_EVENT]) { " + _EVENT_FIELDS + " } } } } }"
)
_TIMELINE_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $cursor: String) {"
    " repository(owner: $owner, name: $name) { issue(number: $number) {"
    " timelineItems(first: 100, after: $cursor, itemTypes: [CLOSED_EVENT, REOPENED_EVENT]) { "
    + _EVENT_FIELDS
    + " } } } }"
)


# --------------------------------------------------------------------------------------------------------------------
# Target resolution and the dedicated clone
# --------------------------------------------------------------------------------------------------------------------


def _toplevel(cwd: Path) -> Path | None:
    """Return the work-tree root containing ``cwd``, or None when ``cwd`` is not inside a git work tree."""
    try:
        out = run_git(cwd, "rev-parse", "--show-toplevel").strip()
    except GitError:
        return None
    return Path(out) if out else None


def _checkout_identity(checkout: Path) -> tuple[list[str], list[str]]:
    """Return the store path components of a checkout (from its ``origin`` URL) and any warnings.

    A checkout without an ``origin`` remote, or with one that is not a recognisable remote URL, gets the identity
    ``["local", <12-hex hash of its absolute path>]``. The URL itself never appears in a warning, because it may carry
    credentials.
    """
    url = run_git(checkout, "config", "--get", "remote.origin.url", check=False).strip()
    parts = parse_remote_url(url) if url else None
    warnings: list[str] = []
    if url and parts is None:
        warnings.append("origin URL of the checkout is not a recognised remote; using a local store identity")
    if parts is None:
        parts = ["local", sha256_text(checkout.resolve().as_posix())[:12]]
    return parts, warnings


def _parse_target(target: str) -> tuple[list[str], str]:
    """Turn an ``OWNER/REPO`` shorthand or a remote URL into store path components and a clone URL.

    Raises:
        ValueError: the target is neither form, or a component starts with ``-`` (would read as a git option).
    """
    text = target.strip()
    if _SHORTHAND.match(text):
        slug = text[: -len(".git")] if text.endswith(".git") else text
        url = f"https://github.com/{slug}.git"
    else:
        url = text
    parts = parse_remote_url(url)
    if parts is None or url.startswith("-") or any(part.startswith("-") for part in parts):
        raise ValueError(f"target {target!r} is neither OWNER/REPO nor a git remote URL")
    return parts, url


def _same_repo(left: list[str], right: list[str]) -> bool:
    """Return True when two store identities name the same repository (hosts and paths compared case-insensitively)."""
    return [part.casefold() for part in left] == [part.casefold() for part in right]


def _run_gh(args: list[str]) -> str | None:
    """Run one read-only ``gh`` command and return its stdout; None when gh is missing, fails or times out.

    This is the only place that starts ``gh``; tests replace it (or :func:`_gh_graphql`) so no test touches the network.
    """
    gh = shutil.which("gh")
    if gh is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            [gh, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GH_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _fork_parent_url(parts: list[str]) -> tuple[str | None, list[str]]:
    """Return the clone URL of a GitHub fork's parent repository and any warnings.

    Read-only: one ``gh repo view OWNER/REPO --json parent`` call. Non-GitHub identities and ``local/*`` identities
    never reach ``gh``. A missing or failing ``gh`` degrades to no upstream with a warning.
    """
    if len(parts) != 3 or parts[0] != "github.com":
        return None, []
    out = _run_gh(["repo", "view", f"{parts[1]}/{parts[2]}", "--json", "parent"])
    if out is None:
        return None, ["gh unavailable or failed; fork upstream not resolved"]
    try:
        parent = json.loads(out).get("parent")
    except (json.JSONDecodeError, AttributeError):
        return None, ["gh returned unreadable repository data; fork upstream not resolved"]
    if not isinstance(parent, dict):
        return None, []
    owner = (parent.get("owner") or {}).get("login")
    name = parent.get("name")
    upstream = f"https://github.com/{owner}/{name}.git" if isinstance(owner, str) and isinstance(name, str) else ""
    if not upstream or parse_remote_url(upstream) is None:
        return None, ["gh returned an unusable fork parent; fork upstream not resolved"]
    return upstream, []


def resolve_target(target: str | None, cwd: Path) -> dict[str, Any]:
    """Decide which repository a scan analyses and whether it needs a dedicated clone.

    Read-only. Rules:

    * ``target`` None: the git work tree containing ``cwd``; store parts from its ``origin`` URL, else
      ``["local", <12-hex hash of the absolute path>]``.
    * ``OWNER/REPO`` shorthand: ``github.com/OWNER/REPO``. A remote URL: parsed by ``parse_remote_url``.
    * When the work tree containing ``cwd`` has an ``origin`` naming the same repository (case-insensitive), that
      checkout is used read-only (``dedicated`` False, its own parts returned so both spellings share one store);
      otherwise the target needs a dedicated clone (``dedicated`` True, ``clone_url`` set).
    * GitHub targets look up their fork parent with ``gh repo view --json parent``; the checkout's remotes and
      config are never touched.

    Args:
        target: None, ``OWNER/REPO`` or a git remote URL.
        cwd: Directory the scan was started from.

    Returns:
        ``{"parts": [host, *path], "checkout": str | None, "dedicated": bool, "clone_url": str | None,
        "upstream_url": str | None, "warnings": [str]}``.

    Raises:
        EvolutionError: ``target`` is None and ``cwd`` is not inside a git work tree.
        ValueError: ``target`` is neither ``OWNER/REPO`` nor a recognisable remote URL.

    Examples:
        >>> # resolve_target("octo/widget", Path.cwd())["clone_url"]  ->  'https://github.com/octo/widget.git'
    """
    checkout = _toplevel(cwd)
    local = _checkout_identity(checkout) if checkout is not None else None
    warnings: list[str] = []
    if target is None:
        if checkout is None or local is None:
            raise EvolutionError(f"{cwd} is not inside a git work tree; pass a target as OWNER/REPO or a remote URL")
        parts, clone_url, dedicated = local[0], None, False
        warnings += local[1]
    else:
        parts, clone_url = _parse_target(target)
        dedicated = local is None or not _same_repo(local[0], parts)
        if not dedicated and local is not None:
            parts, clone_url = local[0], None
    upstream_url, gh_warnings = _fork_parent_url(parts)
    warnings += gh_warnings
    if upstream_url and not dedicated:
        warnings.append("target is a fork; upstream tags are not fetched into the read-only checkout")
    return {
        "parts": parts,
        "checkout": None if dedicated or checkout is None else str(checkout),
        "dedicated": dedicated,
        "clone_url": clone_url,
        "upstream_url": upstream_url,
        "warnings": warnings,
    }


def _ensure_remote(clone_dir: Path, name: str, url: str) -> None:
    """Add remote ``name`` to the dedicated clone, or repoint it when its URL differs."""
    current = run_git(clone_dir, "config", "--get", f"remote.{name}.url", check=False).strip()
    if not current:
        run_git(clone_dir, "remote", "add", name, url)
    elif current != url:
        run_git(clone_dir, "remote", "set-url", name, url)


def _fresh_clone(clone_dir: Path, url: str, timeout: float) -> None:
    """Make a full clone (no depth limit) of ``url`` at ``clone_dir``.

    Raises:
        EvolutionError: ``clone_dir`` exists, is not empty and is not a git clone (never deleted here).
    """
    if clone_dir.exists() and any(clone_dir.iterdir()):
        raise EvolutionError(f"{clone_dir} exists but is not a git clone; remove it and rerun")
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    run_git(clone_dir.parent, "clone", "--quiet", "--origin", "origin", "--", url, clone_dir.name, timeout=timeout)


def _follow_remote_head(clone_dir: Path, timeout: float) -> list[str]:
    """Move the dedicated clone's HEAD to origin's default branch tip (detached); return warnings.

    The clone is owned by the tool, so the checkout is forced. Without this, a later fetch would leave HEAD - and so
    the index's unreleased commits - at the commit of the first clone.
    """
    warnings: list[str] = []
    try:
        run_git(clone_dir, "remote", "set-head", "origin", "--auto", timeout=timeout)
    except GitError as exc:
        warnings.append(f"origin default branch not refreshed: {exc}")
    tip = run_git(clone_dir, "rev-parse", "--verify", "-q", "refs/remotes/origin/HEAD^{commit}", check=False).strip()
    if not tip:
        return [*warnings, "origin has no default branch; HEAD of the dedicated clone left unchanged"]
    run_git(clone_dir, "checkout", "--quiet", "--force", "--detach", tip, timeout=timeout)
    return warnings


def sync_clone(
    clone_dir: Path, url: str, upstream_url: str | None = None, *, timeout: float = NETWORK_TIMEOUT_S
) -> list[str]:
    """Create or refresh the dedicated full clone with every tag; the caller holds the store lock.

    A missing clone is created with ``git clone`` (full history, no depth). Every run then fetches origin with
    ``git fetch --tags --force --prune --prune-tags origin`` (clone alone misses tags on history no branch reaches),
    and, for a fork, fetches the parent's tags into remote ``upstream`` of this clone only, with ``--tags --force`` and
    without ``--prune-tags`` (that would delete origin-only tags). Upstream wins when both define a tag. Finally HEAD
    moves to origin's default branch tip.

    Args:
        clone_dir: Location of the dedicated clone (``StorePaths.repo``).
        url: Clone URL of the target.
        upstream_url: Clone URL of the fork parent, or None.
        timeout: Seconds allowed per clone or fetch call.

    Returns:
        Warnings (an upstream fetch failure is a warning, not an error).

    Raises:
        ValueError: a URL starts with ``-``.
        EvolutionError: ``clone_dir`` is occupied by something that is not a git clone.
        GitError: cloning or fetching origin failed.

    Examples:
        >>> # sync_clone(paths.repo, "https://github.com/octo/widget.git")  ->  []
    """
    if url.startswith("-") or (upstream_url or "").startswith("-"):
        raise ValueError("a clone URL must not start with '-'")
    if (clone_dir / ".git").exists():
        _ensure_remote(clone_dir, "origin", url)
    else:
        _fresh_clone(clone_dir, url, timeout)
    run_git(clone_dir, "fetch", "--quiet", "--tags", "--force", "--prune", "--prune-tags", "origin", timeout=timeout)
    warnings: list[str] = []
    if upstream_url:
        _ensure_remote(clone_dir, "upstream", upstream_url)
        try:
            run_git(clone_dir, "fetch", "--quiet", "--tags", "--force", "--prune", "upstream", timeout=timeout)
        except GitError as exc:
            warnings.append(f"upstream tags not fetched: {exc}")
    return warnings + _follow_remote_head(clone_dir, timeout)


def is_shallow(repo: Path) -> bool:
    """Return True when ``repo`` is a shallow clone (its history, and so the index, would be truncated).

    Examples:
        >>> # is_shallow(Path("."))  ->  False
    """
    return run_git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"


# --------------------------------------------------------------------------------------------------------------------
# Release index
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Tag:
    """One tag, peeled to its commit when it has one.

    Attributes:
        name: Tag name without ``refs/tags/``.
        object_sha: The ref's own object (the tag object for annotated tags).
        commit: Peeled commit, or None when the tag points at a tree or blob.
        timestamp: Unix time: tagger date for annotated tags, committer date for lightweight ones.
    """

    name: str
    object_sha: str
    commit: str | None
    timestamp: int


@dataclass(slots=True)
class _Release:
    """A release while the index is being built; serialised to a plain dict at the boundary."""

    tag: str
    version: Version
    sha: str
    timestamp: int
    line: str = ""
    predecessor: int | None = None


def _bits(mask: int) -> Iterator[int]:
    """Yield the indices of the set bits of ``mask``, lowest first.

    Examples:
        >>> list(_bits(0b10110))
        [1, 2, 4]
    """
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low


def _int_or_none(text: str) -> int | None:
    """Parse a possibly empty or signed integer field from ``for-each-ref``."""
    try:
        return int(text)
    except ValueError:
        return None


def _peel(repo: Path, objects: list[str]) -> dict[str, str]:
    """Peel tag objects to commits in one ``cat-file`` batch; objects that do not peel to a commit are left out."""
    if not objects:
        return {}
    stdin = "".join(f"{sha}^{{commit}}\n" for sha in objects)
    out = run_git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)", stdin=stdin)
    peeled: dict[str, str] = {}
    for sha, line in zip(objects, out.splitlines()):
        fields = line.split()
        if len(fields) == 2 and fields[1] == "commit":
            peeled[sha] = fields[0]
    return peeled


def _commit_times(repo: Path, commits: list[str]) -> dict[str, int]:
    """Return committer times for commits in one ``git log --no-walk`` call."""
    if not commits:
        return {}
    out = run_git(repo, "log", "--no-walk=unsorted", "--stdin", "--format=%H %ct", stdin="\n".join(commits) + "\n")
    times: dict[str, int] = {}
    for line in out.splitlines():
        sha, _, stamp = line.partition(" ")
        value = _int_or_none(stamp)
        if value is not None:
            times[sha] = value
    return times


def _parse_tag_line(line: str) -> tuple[str, str, str | None, int | None, bool] | None:
    """Parse one ``_TAG_FORMAT`` line into ``(name, object, commit, time, is_tag_of_tag)``; None for a foreign line.

    ``commit`` is None when the ref needs a deeper peel or points at a tree or blob; ``time`` falls back from the
    creator date to the one-level peeled committer date and is None when both are absent.
    """
    fields = line.split("\t")
    if len(fields) != 7 or not fields[0].startswith(_TAG_PREFIX):
        return None
    ref, otype, osha, ptype, psha, created, peeled_time = fields
    commit = osha if otype == "commit" else (psha if ptype == "commit" else None)
    stamp = _int_or_none(created)
    if stamp is None:
        stamp = _int_or_none(peeled_time)
    return ref[len(_TAG_PREFIX) :], osha, commit, stamp, ptype == "tag"


def _list_tags(repo: Path) -> list[_Tag]:
    """List every tag with its peeled commit and creation time: one ``for-each-ref`` plus rare peel/time batches.

    ``%(*objectname)`` peels one level only, so tags of tags are peeled through ``cat-file``; annotated tags without a
    tagger line fall back to their commit's committer time.
    """
    out = run_git(repo, "for-each-ref", f"--format={_TAG_FORMAT}", "refs/tags")
    parsed = [row for row in map(_parse_tag_line, out.splitlines()) if row is not None]
    peeled = _peel(repo, [osha for _, osha, _, _, nested in parsed if nested])
    rows = [(name, osha, commit, stamp) for name, osha, commit, stamp, _ in parsed]
    rows = [(name, osha, commit or peeled.get(osha), stamp) for name, osha, commit, stamp in rows]
    missing = sorted({commit for _, _, commit, stamp in rows if commit and stamp is None})
    times = _commit_times(repo, missing)
    return [
        _Tag(name, osha, commit, stamp if stamp is not None else times.get(commit or "", 0))
        for name, osha, commit, stamp in rows
    ]


def _select_releases(tags: list[_Tag]) -> tuple[list[_Release], list[str]]:
    """Keep tags that parse as versions and point at commits; return releases in global order and excluded tags.

    Excluded (reported in ``unparsable_tags``): tags that are not versions, tags not pointing at a commit, and a second
    tag with an already-seen canonical version on a different commit (the earliest tag wins). A second tag with the same
    version on the same commit is an alias and is dropped silently.
    """
    excluded: list[str] = []
    kept: dict[str, _Release] = {}
    for tag in sorted(tags, key=lambda item: (item.timestamp, item.name)):
        version = parse_version(tag.name)
        if version is None or tag.commit is None:
            excluded.append(tag.name)
            continue
        first = kept.get(version.canonical)
        if first is None:
            kept[version.canonical] = _Release(tag.name, version, tag.commit, tag.timestamp)
        elif first.sha != tag.commit:
            excluded.append(tag.name)
    releases = sorted(kept.values(), key=lambda rel: (rel.timestamp, rel.version.sort_key(), rel.tag))
    return releases, sorted(excluded)


def _head_sha(repo: Path) -> str | None:
    """Return the commit HEAD points at, or None for an unborn branch."""
    return run_git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False).strip() or None


def _walk(repo: Path, releases: list[_Release], head: str | None) -> tuple[dict[str, int], list[str]]:
    """Walk history once from every release commit and HEAD; return each commit's release mask and the walk order.

    Bit ``i`` of a commit's mask is set when release ``i`` (global order) contains the commit. ``--topo-order`` lists
    every child before its parents, so a single pass pushing each mask onto the parents is complete. One git call
    replaces a ``rev-list`` per release; start points go through stdin to stay under command-line limits.
    """
    starts = sorted({release.sha for release in releases} | ({head} if head else set()))
    if not starts:
        return {}, []
    own: dict[str, int] = {}
    for index, release in enumerate(releases):
        own[release.sha] = own.get(release.sha, 0) | (1 << index)
    out = run_git(
        repo,
        "rev-list",
        "--topo-order",
        "--parents",
        "--stdin",
        stdin="\n".join(starts) + "\n",
        timeout=_WALK_TIMEOUT_S,
    )
    masks: dict[str, int] = {}
    order: list[str] = []
    for line in out.splitlines():
        shas = line.split()
        if not shas:
            continue
        commit, mask = shas[0], masks.get(shas[0], 0) | own.get(shas[0], 0)
        masks[commit] = mask
        order.append(commit)
        if mask:
            for parent in shas[1:]:
                masks[parent] = masks.get(parent, 0) | mask
    return masks, order


def _assign_predecessors(releases: list[_Release], masks: dict[str, int]) -> None:
    """Set each release's predecessor: its ancestor release with the greatest version key below its own.

    Ancestors are releases whose commit the release contains; two releases on one commit are ordered by global order.
    Preferring the greatest key *below* the release keeps a maintenance release on its line even when the maintenance
    branch merged a newer trunk release (2.0.0 is an ancestor of 1.4.3, but 1.4.2 is its predecessor). When no ancestor
    has a lower key (a renumbered project), the latest ancestor in global order is used.
    """
    ancestors = [0] * len(releases)
    for index, release in enumerate(releases):
        for other in _bits(masks.get(release.sha, 0)):
            if other != index and (releases[other].sha != release.sha or index < other):
                ancestors[other] |= 1 << index
    keys = [release.version.sort_key() for release in releases]
    for index, release in enumerate(releases):
        candidates = list(_bits(ancestors[index]))
        below = [other for other in candidates if keys[other] < keys[index]]
        if below:
            release.predecessor = max(below, key=lambda other: (keys[other], other))
        elif candidates:
            release.predecessor = max(candidates)


def _subtree_best(releases: list[_Release], children: list[list[int]], roots: list[int]) -> list[tuple[Any, int]]:
    """Return, per release, the greatest ``(version key, order)`` found in its predecessor subtree."""
    best: list[tuple[Any, int]] = [(release.version.sort_key(), index) for index, release in enumerate(releases)]
    preorder: list[int] = []
    stack = list(roots)
    while stack:
        node = stack.pop()
        preorder.append(node)
        stack.extend(children[node])
    for node in reversed(preorder):
        for child in children[node]:
            best[node] = max(best[node], best[child])
    return best


def _split_runs(
    children: list[list[int]], roots: list[int], best: list[tuple[Any, int]]
) -> tuple[list[list[int]], dict[int, int]]:
    """Split the predecessor forest into runs and return them with each release's continuation child.

    Each run follows the child whose subtree holds the highest version; every other child starts a later run, so a run
    is always listed after the run it forks from.
    """
    runs: list[list[int]] = []
    successor: dict[int, int] = {}
    pending = sorted(roots)
    while pending:
        node: int | None = pending.pop(0)
        run: list[int] = []
        while node is not None:
            run.append(node)
            kids = sorted(children[node], key=best.__getitem__, reverse=True)
            pending.extend(kids[1:])
            node = kids[0] if kids else None
            if node is not None:
                successor[run[-1]] = node
        runs.append(run)
    return runs, successor


def _fold_point(
    releases: list[_Release], run: list[int], children: list[list[int]], successor: dict[int, int]
) -> int | None:
    """Return the fork release an off-branch run folds into, or None when the run is a release line of its own.

    An off-branch run is a dead end (no line forks from it) whose versions all stay below the release continuing its
    fork point's line, and whose first version shares that release's ``<major>.<minor>`` family. That shape is a
    release tagged on a branch that was squash-merged or discarded (1.3.6 tagged off trunk between trunk's 1.3.5 and
    1.3.9), not a maintenance line; a 1.3.6 hotfix forked at 1.3.5 while trunk moved on to 1.4.0 stays a line.
    """
    fork = releases[run[0]].predecessor
    nxt = successor.get(fork) if fork is not None else None
    if fork is None or nxt is None or any(len(children[index]) > 1 for index in run):
        return None
    limit = releases[nxt].version
    below = all(releases[index].version.sort_key() < limit.sort_key() for index in run)
    same_family = _line_family(releases[run[0]].version) == _line_family(limit)
    return fork if below and same_family else None


def _line_family(version: Version) -> str:
    """Return the ``<major>.<minor>.x`` family name of a version.

    Examples:
        >>> _line_family(parse_version("1.4.1")), _line_family(parse_version("7"))
        ('1.4.x', '7.x')
    """
    return ".".join(str(part) for part in version.release[:2]) + ".x"


def _name_runs(releases: list[_Release], runs: list[list[int]], top: int) -> list[str]:
    """Name each run: the run holding release ``top`` is the trunk; others take their first release's family.

    Family collisions get ``~2``, ``~3`` suffixes in the global order of each run's first release.
    """
    names = [""] * len(runs)
    used: dict[str, int] = {}
    for run_index in sorted(range(len(runs)), key=lambda item: runs[item][0]):
        if top in runs[run_index]:
            names[run_index] = TRUNK_LINE
            continue
        base = _line_family(releases[runs[run_index][0]].version)
        used[base] = used.get(base, 0) + 1
        names[run_index] = base if used[base] == 1 else f"{base}~{used[base]}"
    return names


def _assign_lines(releases: list[_Release]) -> dict[str, list[int]]:
    """Assign every release a home line and return each line's full chain of release indices, oldest first.

    A line's chain is the chain of the line it forked from, up to and including the fork point, followed by its own
    releases; off-branch runs (see :func:`_fold_point`) join the line they forked from, after the fork point in version
    order.
    """
    children: list[list[int]] = [[] for _ in releases]
    roots: list[int] = []
    for index, release in enumerate(releases):
        (roots if release.predecessor is None else children[release.predecessor]).append(index)
    if not roots:
        return {}
    best = _subtree_best(releases, children, roots)
    runs, successor = _split_runs(children, roots, best)
    top = max(best[root] for root in roots)[1]
    folded: dict[int, list[int]] = {}
    lines: list[list[int]] = []
    for run in runs:
        fork = None if top in run else _fold_point(releases, run, children, successor)
        if fork is None:
            lines.append(run)
        else:
            folded.setdefault(fork, []).extend(run)
    chains: dict[str, list[int]] = {}
    for name, run in zip(_name_runs(releases, lines, top), lines):
        own = _with_folded(releases, run, folded)
        for index in own:
            releases[index].line = name
        fork = releases[run[0]].predecessor
        parent = [] if fork is None else chains[releases[fork].line]
        chains[name] = [*parent[: parent.index(fork) + 1 if parent else 0], *own]
    return chains


def _with_folded(releases: list[_Release], run: list[int], folded: dict[int, list[int]]) -> list[int]:
    """Return a run with the off-branch releases folded into it, each placed after its fork point in version order."""
    own: list[int] = []
    for node in run:
        own.append(node)
        own.extend(sorted(folded.get(node, []), key=lambda index: releases[index].version.sort_key()))
    return own


def _commit_releases(
    releases: list[_Release], chains: dict[str, list[int]], masks: dict[str, int]
) -> dict[str, dict[str, str]]:
    """Map every released commit to the first release on each line (chain) that contains it.

    Where every chain release contains the previous one, the first containing release is the ``git rev-list r ^prev``
    range the commit falls in. Off-branch releases on a chain make that containment partial, and the first-containing
    rule still names the release that first shipped the commit on the line. Commits sharing a mask share the answer,
    so it is computed once per distinct mask.
    """
    steps = {
        name: [(1 << index, releases[index].version.canonical) for index in chain] for name, chain in chains.items()
    }
    memo: dict[int, dict[str, str]] = {}
    result: dict[str, dict[str, str]] = {}
    for commit, mask in masks.items():
        if not mask:
            continue
        entry = memo.get(mask)
        if entry is None:
            entry = {}
            for name, chain_steps in steps.items():
                first = next((version for bit, version in chain_steps if mask & bit), None)
                if first is not None:
                    entry[name] = first
            memo[mask] = entry
        result[commit] = dict(entry)
    return result


def _iso_utc(timestamp: int) -> str:
    """Render a unix time as ISO-8601 UTC with a ``Z`` suffix.

    Examples:
        >>> _iso_utc(0)
        '1970-01-01T00:00:00Z'
    """
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_release_index(repo: Path) -> dict[str, Any]:
    """Build the release index of ``repo``: every release tag, its release line, predecessor and commit coverage.

    Algorithm:

    1. **Tags**: one ``for-each-ref`` lists every tag. A tag becomes a release when ``parse_version`` accepts its name
       and it peels to a commit; every other tag (and a later tag repeating a version on another commit) is listed in
       ``unparsable_tags``. ``date`` is the tagger date of an annotated tag, else the commit's committer date.
    2. **Order**: global ``order`` sorts by date, then version key, then tag name (0 = oldest).
    3. **Ancestry**: one ``rev-list --topo-order --parents`` walk from all release commits and HEAD gives each commit
       the set of releases containing it (a bitmask), and so the ancestry between releases.
    4. **Predecessor**: the ancestor release with the greatest version key below the release's own (see
       :func:`_assign_predecessors`); None for the oldest release of a history.
    5. **Lines**: predecessor links form a tree. A release continues its predecessor's line when its subtree holds the
       highest version among its siblings; every other child starts a new line. The line through the highest version
       overall is ``"main"``; every other line is named ``"<major>.<minor>.x"`` after its first own release, with
       ``~2``, ``~3`` on collisions. Release records carry their home line.
       **Off-branch releases** are not lines: a dead-end run whose versions stay below the release continuing its
       fork point's line, in that release's ``<major>.<minor>`` family (a release tagged on a release branch that was
       squash-merged), joins the fork point's line after the fork point in version order (:func:`_fold_point`).
    6. **Chains**: ``lines[name]`` is the line's full chain, oldest first: the chain of the line it forked from, up to
       and including the fork point, then its own releases. A trunk release therefore appears in the chains of the
       lines forked after it, so per-line age counting works for maintenance fixes of old code.
    7. **Commit coverage**: ``commit_releases[sha][line]`` is the first release along ``lines[line]`` that contains
       the commit; where each chain release contains the previous one, that is the ``git rev-list <release> ^<prev>``
       range the commit falls in. Only commits reachable from some release appear. ``head.unreleased`` lists commits
       reachable from HEAD and from no release, newest first.

    ``predecessor`` stays the ancestry relation of step 4, so an off-branch release is never the predecessor of the
    trunk release that follows it in its chain.

    Example lines: a 1.4.x maintenance branch cut at 1.4.0 while trunk released 2.0.0 gives ``{"main": [..., "1.4.0",
    "2.0.0"], "1.4.x": [..., "1.4.0", "1.4.1"]}``; a feature committed for 2.0.0 and cherry-picked into 1.4.3 has two
    SHAs, mapped to ``{"main": "2.0.0"}`` and ``{"1.4.x": "1.4.3"}``.

    Args:
        repo: Repository root (full history; callers block shallow clones with :func:`is_shallow` first).

    Returns:
        ``{"schema_version", "releases", "unparsable_tags", "lines", "predecessor", "commit_releases", "head",
        "fingerprint"}`` as defined by the evolution contract. ``head["sha"]`` is None on an unborn branch.
        ``fingerprint`` covers the full tag -> object map and the index algorithm version; it does not cover HEAD.

    Examples:
        >>> # build_release_index(Path("."))["lines"]  ->  {'main': ['1.0.0', '1.1.0']}
    """
    tags = _list_tags(repo)
    releases, unparsable = _select_releases(tags)
    head = _head_sha(repo)
    masks, walk_order = _walk(repo, releases, head)
    _assign_predecessors(releases, masks)
    chains = _assign_lines(releases)
    records = [
        {
            "tag": rel.tag,
            "version": rel.version.canonical,
            "sha": rel.sha,
            "date": _iso_utc(rel.timestamp),
            "line": rel.line,
            "is_prerelease": rel.version.is_prerelease,
            "order": index,
        }
        for index, rel in enumerate(releases)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "releases": records,
        "unparsable_tags": unparsable,
        "lines": {name: [releases[i].version.canonical for i in chain] for name, chain in chains.items()},
        "predecessor": {
            rel.version.canonical: None if rel.predecessor is None else releases[rel.predecessor].version.canonical
            for rel in releases
        },
        "commit_releases": _commit_releases(releases, chains, masks),
        "head": {"sha": head, "unreleased": [commit for commit in walk_order if not masks.get(commit)]},
        "fingerprint": fingerprint(ALGO_VERSIONS["index"], {tag.name: tag.object_sha for tag in tags}),
    }


# --------------------------------------------------------------------------------------------------------------------
# Open-bug series (GitHub only, approximate)
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _IssueHistory:
    """Creation time and state changes of one bug issue.

    Attributes:
        created: When the issue was opened.
        events: ``(time, open_after)`` pairs from close and reopen events, oldest first.
    """

    created: datetime
    events: tuple[tuple[datetime, bool], ...]


def _gh_graphql(query: str, variables: dict[str, str | int | None]) -> dict[str, Any] | None:
    """Send one GraphQL query through ``gh api graphql`` and return ``data``; None on any failure or GraphQL error.

    Integers go as typed ``-F`` fields, strings as raw ``-f`` fields; None variables are omitted (GraphQL null).
    """
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is not None:
            args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    out = _run_gh(args)
    if out is None:
        return None
    try:
        body = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict) or body.get("errors"):
        return None
    data = body.get("data")
    return data if isinstance(data, dict) else None


def _dig(data: Any, path: tuple[str, ...]) -> dict[str, Any] | None:
    """Follow ``path`` through nested dicts; None when any step is missing or not a dict."""
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def _paginate(
    query: str, variables: dict[str, str | int | None], path: tuple[str, ...], cursor: str | None = None
) -> list[dict[str, Any]] | None:
    """Collect every node of one GraphQL connection with a cursor loop; None when any page fails.

    Never returns a partial list: a failed page, a missing connection or a repeated cursor fails the whole fetch.
    """
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    while True:
        connection = _dig(_gh_graphql(query, {**variables, "cursor": cursor}), path)
        if connection is None:
            return None
        nodes += [node for node in connection.get("nodes") or [] if isinstance(node, dict)]
        info = connection.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return nodes
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or cursor in seen:
            return None
        seen.add(cursor)


def _complete_timeline(owner: str, repo: str, issue: dict[str, Any]) -> bool:
    """Fetch the remaining close/reopen events of an issue with more than one page of them; False on failure."""
    timeline = issue.get("timelineItems") or {}
    info = timeline.get("pageInfo") or {}
    if not info.get("hasNextPage"):
        return True
    number = issue.get("number")
    if not isinstance(number, int):
        return False
    variables: dict[str, str | int | None] = {"owner": owner, "name": repo, "number": number}
    rest = _paginate(_TIMELINE_QUERY, variables, ("repository", "issue", "timelineItems"), info.get("endCursor"))
    if rest is None:
        return False
    timeline["nodes"] = [*(timeline.get("nodes") or []), *rest]
    issue["timelineItems"] = timeline
    return True


def _collect_issues(owner: str, repo: str, labels: list[str]) -> dict[int, dict[str, Any]] | None:
    """Fetch every issue carrying any of ``labels`` (exact repository label names), keyed by number.

    One fully paginated query per label, merged by issue number, so the result never depends on how GitHub combines
    several labels in one filter.
    """
    issues: dict[int, dict[str, Any]] = {}
    for label in labels:
        variables: dict[str, str | int | None] = {"owner": owner, "name": repo, "label": label}
        fetched = _paginate(_ISSUES_QUERY, variables, ("repository", "issues"))
        if fetched is None:
            return None
        for issue in fetched:
            if isinstance(issue.get("number"), int) and issue["number"] not in issues:
                if not _complete_timeline(owner, repo, issue):
                    return None
                issues[issue["number"]] = issue
    return issues


def _parse_time(text: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` suffix accepted on Python 3.10) into an aware datetime.

    Examples:
        >>> _parse_time("2024-01-02T03:04:05Z").isoformat()
        '2024-01-02T03:04:05+00:00'
    """
    if not isinstance(text, str) or not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _issue_history(issue: dict[str, Any]) -> _IssueHistory | None:
    """Turn one fetched issue into its creation time and state changes; None without a creation time.

    An issue closed without any close event in its timeline falls back to ``closedAt``.
    """
    created = _parse_time(issue.get("createdAt"))
    if created is None:
        return None
    events: list[tuple[datetime, bool]] = []
    for node in (issue.get("timelineItems") or {}).get("nodes") or []:
        when = _parse_time(node.get("createdAt"))
        kind = node.get("__typename")
        if when is not None and kind in _EVENT_STATE:
            events.append((when, _EVENT_STATE[kind]))
    closed_at = _parse_time(issue.get("closedAt"))
    if not any(not state for _, state in events) and closed_at is not None:
        events.append((closed_at, False))
    return _IssueHistory(created, tuple(sorted(events)))


def _open_at(history: _IssueHistory, when: datetime) -> bool:
    """Return True when the issue existed and was open at ``when`` (events at exactly ``when`` count)."""
    if history.created > when:
        return False
    state = True
    for at, opened in history.events:
        if at > when:
            break
        state = opened
    return state


def fetch_open_bug_series(
    owner: str, repo: str, releases: list[dict[str, Any]], labels: list[str] | tuple[str, ...]
) -> dict[str, Any] | None:
    """Count open bug issues at each release date (GitHub only; an approximation).

    Bug issues are those currently carrying any repository label that matches ``labels`` case-insensitively; label
    history is not reconstructed, hence ``approximate``. Open state at a release date replays the issue's
    ``CLOSED_EVENT``/``REOPENED_EVENT`` timeline. Every connection is fully paginated with a cursor loop.

    Args:
        owner: Repository owner.
        repo: Repository name.
        releases: Release records (``version`` and ``date`` are read), in the order the series should follow.
        labels: Requested bug labels, e.g. ``DEFAULT_BUG_LABELS``.

    Returns:
        ``{"approximate": True, "requested_labels": [str], "labels": [matched repository label names],
        "issue_count": int, "series": [{"release": version, "date": iso, "open": int}], "warnings": [str]}``, or None
        when ``gh`` is missing or any request fails (never a partial series).

    Examples:
        >>> # fetch_open_bug_series("octo", "widget", index["releases"], DEFAULT_BUG_LABELS)["series"][0]["open"]
    """
    repo_labels = _paginate(_LABELS_QUERY, {"owner": owner, "name": repo}, ("repository", "labels"))
    if repo_labels is None:
        return None
    matched = _match_labels(repo_labels, labels)
    issues = _collect_issues(owner, repo, matched)
    if issues is None:
        return None
    histories = [history for history in map(_issue_history, issues.values()) if history is not None]
    warnings: list[str] = []
    if not matched:
        warnings.append("no repository label matches the bug label set; the series is all zeros")
    if len(histories) < len(issues):
        warnings.append(f"{len(issues) - len(histories)} bug issues without a creation time were skipped")
    return {
        "approximate": True,
        "requested_labels": list(labels),
        "labels": matched,
        "issue_count": len(histories),
        "series": [_series_point(release, histories) for release in releases],
        "warnings": warnings,
    }


def _match_labels(repo_labels: list[dict[str, Any]], labels: list[str] | tuple[str, ...]) -> list[str]:
    """Return the repository label names matching any requested label, compared case-insensitively.

    Examples:
        >>> _match_labels([{"name": "Bug"}, {"name": "docs"}], ["bug", "kind/bug"])
        ['Bug']
    """
    wanted = {label.strip().casefold() for label in labels if label.strip()}
    names = {node.get("name") for node in repo_labels}
    return sorted(name for name in names if isinstance(name, str) and name.casefold() in wanted)


def _series_point(release: dict[str, Any], histories: list[_IssueHistory]) -> dict[str, Any]:
    """Return ``{"release", "date", "open"}`` for one release; an unreadable release date counts zero."""
    when = _parse_time(release.get("date"))
    count = 0 if when is None else sum(_open_at(history, when) for history in histories)
    return {"release": release.get("version"), "date": release.get("date"), "open": count}


# --------------------------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser with the ``index`` and ``target`` subcommands."""
    parser = argparse.ArgumentParser(
        prog="evolution_releases.py",
        description="Release index and target resolution for /oss:analyse evolution.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    index = sub.add_parser("index", help="build the release index of a repository and write it as JSON")
    index.add_argument("--repo", type=Path, required=True, help="repository root (full clone)")
    index.add_argument("--out", type=Path, required=True, help="output JSON file")
    target = sub.add_parser("target", help="resolve the scan target and print it as JSON")
    target.add_argument("--target", default=None, help="OWNER/REPO or a remote URL (default: the current checkout)")
    return parser


def _cmd_index(repo: Path, out: Path) -> int:
    """Write the release index of ``repo`` to ``out``; return the exit code."""
    if is_shallow(repo):
        print(
            f"! BLOCKED — {repo} is a shallow clone; run `git -C {repo} fetch --unshallow` and rerun", file=sys.stderr
        )
        return EXIT_BLOCKED
    index = build_release_index(repo)
    atomic_write_json(out, index)
    print(
        f"index: {len(index['releases'])} releases, {len(index['lines'])} lines, "
        f"{len(index['unparsable_tags'])} unparsable tags, {len(index['head']['unreleased'])} unreleased commits"
        f" -> {out}"
    )
    if not index["releases"]:
        print("! BLOCKED — no tag parses as a release version; nothing to analyse", file=sys.stderr)
        return EXIT_BLOCKED
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "index":
            return _cmd_index(args.repo, args.out)
        print(json.dumps(resolve_target(args.target, Path.cwd()), sort_keys=True))
        return 0
    except (EvolutionError, ValueError, OSError) as exc:
        print(f"! ERROR — {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
