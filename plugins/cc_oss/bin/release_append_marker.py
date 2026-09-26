#!/usr/bin/env python
"""release_append_marker.py — persist and resolve the /oss:release notes --append baseline.

``--append`` needs to know where the *previous* ``notes``/``prepare`` run left
off, so the next invocation only re-classifies commits landed since then
instead of re-deriving the full ``$LAST_TAG..HEAD`` range. The marker is a
single commit SHA per branch, stored under the project's ``.temp/`` dir
rather than a git-tracked path — losing it degrades safely to the existing
non-append behaviour (full range, full DRAFT.md overwrite), never to a
broken or duplicated draft.

Marker location: ``.temp/release-state-v2/<branch-key>/marker`` — deliberately
*not* date-stamped like this skill's other ``.temp/release-*-$BRANCH-$DATE``
artifacts, since it must survive across days/sessions, not just one run.
``.temp/`` is gitignored (see plugins/CLAUDE.md "Contributor email privacy"
convention already used by this skill) and is documented TTL-managed at
~30 days; on loss, ``resolve`` falls back to ``$LAST_TAG..HEAD`` and the
caller treats it as a fresh append baseline (see SKILL.md ``--append`` flag
docs) — a graceful reset, not data loss. The branch key includes the full
SHA-256 of the raw Git ref; ambiguous legacy state requires a migration decision.

**Two invalidation checks, both required for "valid"**:

1. **Reachability** (``_is_valid_commit``) — the command
   ``git merge-base --is-ancestor <sha> HEAD``, not ``git cat-file -e``. The latter only tests object-database
   existence; a commit orphaned by rebase/force-push stays reflog-protected
   (~90 days by default) and would still report "exists", silently
   re-including already-drafted rewritten-SHA commits in the "incremental"
   range instead of falling back to full-overwrite.
2. **Tag supersession** (``_tag_advanced_past``) — a release tag cut between
   two ``--append`` runs (via ``prepare`` or external ``git tag``) makes an
   otherwise-reachable marker stale: ``<marker>..HEAD`` would straddle the tag
   boundary and re-draft already-released commits. When the tag lands at or
   after the marker, fall back to ``<last_tag>..HEAD`` instead.

Marker commands refuse a pending per-branch publication journal. An advanced
marker cannot certify a transaction that still needs promotion or rollback;
the release workflow recovers a pending append before invoking the guard.

Subcommands:
    is-valid  Print "true"/"false" — does a marker exist, resolve to a
              commit still an ancestor of HEAD, AND sit at/after ``--last-tag``
              (not superseded by a later release cut)?
    resolve   Print the RANGE to use for ``--append`` (marker..HEAD when valid per
              both checks above, else <last-tag>..HEAD); prints an info/warn
              note to stderr.
    receipt  Record the completed range and exact draft bytes after truth review.
    write     Persist the completed notes range endpoint as the marker only for
              the attached branch and a marker path without symlinks.

Usage:
    release_append_marker.py guard --branch <raw-ref>
    release_append_marker.py is-valid --branch <raw-ref> --last-tag <tag>
    release_append_marker.py resolve --branch <raw-ref> --last-tag <tag>
    release_append_marker.py receipt --branch <raw-ref> --last-tag <tag>
        --range-file <path> --draft <path> --output <path>
    release_append_marker.py write --branch <raw-ref> --last-tag <tag> --sha <sha> --receipt <path> --draft <path>

Exit codes:
    0 — success (is-valid and resolve print their result)
    1 — ambiguous legacy state, detached/mismatched branch, or unsafe marker path
    2 — bad/missing required argument (argparse default)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from shutil import which
from urllib.parse import quote

# git argv guard: sha reaches `git merge-base --is-ancestor <sha> ...` argv,
# so a value starting with '-' would be parsed as an option. Not applied to
# last_tag — real tags (e.g. "release-2024-05") aren't hex, and --end-of-options
# below already neutralizes the injection risk for that position.
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def branch_state_key(branch_ref: str) -> str:
    """Name persistent branch state with a bounded label and the full raw-ref digest.

    Args:
        branch_ref: Exact Git branch ref before scratch-slug normalization.

    Returns:
        Portable key whose digest distinguishes refs with the same visible label.

    Raises:
        ValueError: If the ref is empty or contains path-control characters.
    """
    if not branch_ref or any(char in branch_ref for char in "\x00\r\n\\"):
        raise ValueError("invalid raw release branch ref")
    label = re.sub(r"[^a-z0-9]+", "-", branch_ref.lower()).strip("-")[:24].rstrip("-") or "branch"
    digest = hashlib.sha256(branch_ref.encode("utf-8")).hexdigest()
    return f"{label}-{digest}"


def state_relative(branch_ref: str, filename: str) -> str:
    """Return a branch-bound release state path under the versioned namespace.

    Args:
        branch_ref: Exact Git branch ref.
        filename: One of the three persistent release-state filenames.

    Returns:
        Repository-relative path safe on POSIX and Windows.
    """
    if filename not in ("marker", "provenance.json", "journal.json"):
        raise ValueError("invalid release state filename")
    return f".temp/release-state-v2/{branch_state_key(branch_ref)}/{filename}"


def refuse_legacy(root: Path, branch_ref: str) -> None:
    """Stop when an ambiguous legacy marker, provenance, or journal still exists.

    Args:
        root: Repository root containing the ``.temp`` directory.
        branch_ref: Exact Git branch ref.

    Raises:
        ValueError: If migration of an existing legacy file needs a human decision.
    """
    branch_state_key(branch_ref)
    raw_slug = branch_ref.replace("/", "-")
    slugs = {raw_slug, quote(raw_slug, safe="-_.+")}
    for slug in sorted(slugs):
        for name in (
            f"release-last-processed-{slug}",
            f"release-provenance-{slug}.json",
            f"release-append-publish-{slug}.json",
        ):
            legacy = root / ".temp" / name
            if legacy.exists() or legacy.is_symlink():
                raise ValueError(f"legacy release state exists at {legacy}; explicit migration decision required")


def _marker_path(branch: str, marker_dir: str | None) -> Path:
    """Resolve the marker file path for a branch.

    Args:
        branch: Raw Git branch ref.
        marker_dir: Override directory (tests / non-default layouts); defaults
            to ``.temp`` under the current working directory.

    Examples:
        >>> _marker_path("main", "/opt/x").name
        'marker'
    """
    base = Path(marker_dir) if marker_dir else Path(".temp")
    return base / "release-state-v2" / branch_state_key(branch) / "marker"


def _refuse_linked_marker(branch: str, marker_dir: str | None) -> None:
    """Reject a marker path that traverses a symlink before reading or falling back."""
    path = _marker_path(branch, marker_dir)
    if any(component.is_symlink() for component in (path, *path.parents)):
        raise ValueError("release marker path must not traverse a symlink")


def _is_valid_commit(sha: str) -> bool:
    """Return True when ``sha`` is an ancestor of HEAD in this repo.

    Uses ``git merge-base --is-ancestor`` (reachability from HEAD), not
    ``git cat-file -e`` (mere object-database existence) — a rebased/reset-away
    commit stays reflog-protected (~90 days by default ``gc.reflogExpire``) so
    ``cat-file -e`` would report it "valid" long after it stopped being real
    history, silently re-including already-drafted rewritten-SHA commits in
    the "incremental" range instead of falling back to full-overwrite.

    Args:
        sha: Candidate commit SHA read from the marker file.

    Returns:
        False for an empty/blank sha, when ``sha`` isn't a bare-hex commit id
        (argument-injection guard), when git is unavailable, or when ``sha``
        is not (or no longer) an ancestor of HEAD (e.g. after a force-push or
        rebase rewrote history and orphaned it).
    """
    if not sha:
        return False
    if not _SHA_RE.match(sha):
        return False
    git = which("git")
    if git is None:
        return False
    result = subprocess.run(  # noqa: S603
        [git, "merge-base", "--is-ancestor", "--end-of-options", sha, "HEAD"],
        capture_output=True,
        check=False,
        timeout=5,
    )
    return result.returncode == 0


def _tag_advanced_past(marker_sha: str, last_tag: str) -> bool:
    """Return True when ``last_tag`` was cut at or after ``marker_sha``.

    A release tag landing between two ``--append`` runs (via ``prepare`` or
    external ``git tag``) makes a still-valid marker stale: trusting it would
    compute ``<marker>..HEAD``, which straddles the tag boundary and re-drafts
    commits that already shipped in that release. When the marker is at or
    behind the tag, the caller should prefer ``<last_tag>..HEAD`` instead.

    Args:
        marker_sha: The stored marker commit.
        last_tag: Tag ref/name (or any revision git can resolve) to compare against.

    Returns:
        True when ``marker_sha`` is an ancestor of (or equal to) ``last_tag``
        — a release was cut at/after the marker. False when ``last_tag`` is
        empty, ``marker_sha`` isn't a bare-hex commit id (argument-injection
        guard), git is unavailable, ``last_tag`` doesn't resolve (e.g. no
        stable tags yet), or ``last_tag`` predates the marker (the normal,
        safe case — nothing to do).
    """
    if not marker_sha or not last_tag:
        return False
    if not _SHA_RE.match(marker_sha):
        return False
    git = which("git")
    if git is None:
        return False
    result = subprocess.run(  # noqa: S603
        [git, "merge-base", "--is-ancestor", "--end-of-options", marker_sha, last_tag],
        capture_output=True,
        check=False,
        timeout=5,
    )
    return result.returncode == 0


def _read_marker(branch: str, marker_dir: str | None) -> str:
    """Read the stored marker sha for a branch, or "" if absent/unreadable.

    Args:
        branch: Raw Git branch ref.
        marker_dir: Override directory (see :func:`_marker_path`).

    Returns:
        Stripped sha string, or "" when the marker file is missing/unreadable.
    """
    try:
        return _marker_path(branch, marker_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def cmd_is_valid(args: argparse.Namespace) -> int:
    """Print "true"/"false" for whether a usable marker exists.

    Requires ``--last-tag`` too (not just ancestor-of-HEAD) so this agrees
    with ``resolve``'s RANGE computation — a marker superseded by a later tag
    must report "false" here as well, or the Write-release-draft phase would
    merge-mode a DRAFT.md whose Gather-changes phase actually used the full
    ``$LAST_TAG..HEAD`` range (mismatch between what was gathered and how it
    gets written).

    Args:
        args: Namespace with ``branch``, ``last_tag``, ``marker_dir``.

    Returns:
        Always 0.
    """
    sha = _read_marker(args.branch, args.marker_dir)
    valid = _is_valid_commit(sha) and not _tag_advanced_past(sha, args.last_tag)
    print("true" if valid else "false")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    """Print the ``--append`` RANGE to stdout; print an info/warn note to stderr.

    Args:
        args: Namespace with ``branch``, ``last_tag``, ``marker_dir``.

    Returns:
        Always 0.
    """
    sha = _read_marker(args.branch, args.marker_dir)
    valid = _is_valid_commit(sha)
    superseded = valid and _tag_advanced_past(sha, args.last_tag)
    if valid and not superseded:
        print(f"ℹ append: resuming from marker {sha[:12]} (incremental range)", file=sys.stderr)
        print(f"{sha}..HEAD")
        return 0
    if superseded:
        print(
            f"⚠ append: {args.last_tag} was cut at/after marker {sha[:12]} — falling back to "
            f"{args.last_tag}..HEAD (marker superseded by a release tag)",
            file=sys.stderr,
        )
    elif sha:
        print(
            f"⚠ append: marker sha {sha[:12]} not found in history (rebase/force-push?)"
            f" — falling back to {args.last_tag}..HEAD",
            file=sys.stderr,
        )
    else:
        print(
            f"ℹ append: no prior marker — establishing first append baseline from {args.last_tag}..HEAD",
            file=sys.stderr,
        )
    print(f"{args.last_tag}..HEAD")
    return 0


def _range_start_on_endpoint(git: str, start: str, endpoint: str) -> str | None:
    """Resolve a range start and require it to precede the completed endpoint."""
    resolved = subprocess.run(  # noqa: S603
        [git, "rev-parse", "--verify", "--end-of-options", f"{start}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if resolved.returncode != 0:
        return None
    start_sha = resolved.stdout.strip()
    ancestor = subprocess.run(  # noqa: S603
        [git, "merge-base", "--is-ancestor", "--end-of-options", start_sha, endpoint],
        capture_output=True,
        check=False,
        timeout=5,
    )
    return start_sha if ancestor.returncode == 0 else None


def unique_root_commit(root: Path, git: str) -> str:
    """Require one root commit as the first release baseline without marker or tag."""
    roots = subprocess.run(  # noqa: S603
        [git, "rev-list", "--max-parents=0", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    values = roots.stdout.splitlines()
    if roots.returncode != 0 or len(values) != 1 or not _SHA_RE.fullmatch(values[0]):
        raise ValueError("first release baseline needs one reachable root commit")
    return values[0]


def _live_baseline(branch: str, last_tag: str, endpoint: str, git: str) -> tuple[str | None, str | None, str | None]:
    """Bind the live saved marker and selected tag to one reachable notes baseline."""
    _refuse_linked_marker(branch, None)
    marker = _marker_path(branch, None)
    marker_bytes = marker.read_bytes() if marker.is_file() else None
    marker_digest = hashlib.sha256(marker_bytes).hexdigest() if marker_bytes is not None else None
    saved = marker_bytes.decode("utf-8").strip() if marker_bytes is not None else ""
    saved_sha = saved if _is_valid_commit(saved) else None
    tag_sha = None
    if last_tag:
        tag = subprocess.run(  # noqa: S603
            [git, "rev-parse", "--verify", "--end-of-options", f"{last_tag}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if tag.returncode != 0 or not _is_valid_commit(tag.stdout.strip()):
            raise ValueError("selected release tag is unavailable on HEAD")
        tag_sha = tag.stdout.strip()
    baseline = tag_sha if saved_sha and tag_sha and _range_start_on_endpoint(git, saved_sha, tag_sha) else saved_sha
    baseline = baseline or tag_sha or unique_root_commit(Path.cwd(), git)
    if baseline and _range_start_on_endpoint(git, baseline, endpoint) is None:
        raise ValueError("completed range endpoint is behind the current baseline")
    return marker_digest, tag_sha, baseline


def cmd_receipt(args: argparse.Namespace) -> int:
    """Bind the selected notes range to current source and exact draft bytes."""
    try:
        lines = Path(args.range_file).read_text(encoding="utf-8").splitlines()
        draft_bytes = Path(args.draft).read_bytes()
        if len(lines) != 1 or lines[0].count("..") != 1 or "..." in lines[0] or not draft_bytes:
            raise ValueError("invalid completed notes range or draft")
        start, end = lines[0].split("..")
        if not start or not end:
            raise ValueError("invalid completed notes range")
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    git = which("git")
    if git is None:
        print("Error: git unavailable for release marker receipt", file=sys.stderr)
        return 1
    branch = subprocess.run(  # noqa: S603
        [git, "symbolic-ref", "--quiet", "--short", "HEAD"], capture_output=True, text=True, check=False, timeout=5
    )
    head = subprocess.run(  # noqa: S603
        [git, "rev-parse", "--verify", "HEAD^{commit}"], capture_output=True, text=True, check=False, timeout=5
    )
    endpoint = subprocess.run(  # noqa: S603
        [git, "rev-parse", "--verify", "--end-of-options", f"{end}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if branch.returncode or branch.stdout.strip() != args.branch or head.returncode or endpoint.returncode:
        print("Error: completed notes source is unavailable or on another branch", file=sys.stderr)
        return 1
    if not _is_valid_commit(endpoint.stdout.strip()):
        print("Error: completed notes endpoint is not on HEAD", file=sys.stderr)
        return 1
    start_sha = _range_start_on_endpoint(git, start, endpoint.stdout.strip())
    if start_sha is None:
        print("Error: completed notes start is not an ancestor of endpoint", file=sys.stderr)
        return 1
    try:
        marker_digest, tag_sha, baseline = _live_baseline(args.branch, args.last_tag, endpoint.stdout.strip(), git)
        if baseline and _range_start_on_endpoint(git, start_sha, baseline) is None:
            raise ValueError("completed notes range skips the current baseline")
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    receipt = {
        "branch": args.branch,
        "range": f"{start_sha}..{endpoint.stdout.strip()}",
        "head": head.stdout.strip(),
        "endpoint": endpoint.stdout.strip(),
        "draft_sha256": hashlib.sha256(draft_bytes).hexdigest(),
        "last_tag": args.last_tag,
        "tag_sha": tag_sha,
        "saved_marker_sha256": marker_digest,
        "baseline": baseline,
    }
    try:
        Path(args.output).write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    except OSError as error:
        print(f"Error: release marker receipt write failed: {error}", file=sys.stderr)
        return 1
    return 0


def cmd_write(args: argparse.Namespace) -> int:
    """Persist only an endpoint bound to a completed range and unchanged draft.

    Args:
        args: Namespace with ``branch``, ``sha``, receipt, draft, and ``marker_dir``.

    Returns:
        Zero on success; one for detached/mismatched branches or unsafe paths.
    """
    if not args.receipt or not args.draft:
        print("Error: release marker write requires completed notes receipt and draft", file=sys.stderr)
        return 1
    try:
        receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
        draft_bytes = Path(args.draft).read_bytes()
        if not isinstance(receipt, dict) or not draft_bytes or not _SHA_RE.fullmatch(args.sha):
            raise ValueError("invalid completed notes receipt or draft")
        completed_range = receipt.get("range")
        if not isinstance(completed_range, str) or completed_range.count("..") != 1 or "..." in completed_range:
            raise ValueError("invalid completed notes range")
        start, end = completed_range.split("..")
        if not start or not end or receipt.get("branch") != args.branch:
            raise ValueError("invalid completed notes branch or range")
        if (
            receipt.get("endpoint") != args.sha
            or receipt.get("draft_sha256") != hashlib.sha256(draft_bytes).hexdigest()
        ):
            raise ValueError("completed notes receipt does not match marker or draft")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    git = which("git")
    if git is None:
        print("Error: git unavailable for release marker write", file=sys.stderr)
        return 1
    branch = subprocess.run(  # noqa: S603
        [git, "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if branch.returncode != 0 or branch.stdout.strip() != args.branch:
        print("Error: release marker requires the current attached branch", file=sys.stderr)
        return 1
    head = subprocess.run(  # noqa: S603
        [git, "rev-parse", "--verify", "HEAD^{commit}"], capture_output=True, text=True, check=False, timeout=5
    )
    endpoint = subprocess.run(  # noqa: S603
        [git, "rev-parse", "--verify", "--end-of-options", f"{end}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if (
        head.returncode != 0
        or head.stdout.strip() != receipt.get("head")
        or endpoint.returncode != 0
        or endpoint.stdout.strip() != args.sha
        or not _is_valid_commit(args.sha)
    ):
        print("Error: marker SHA is not the completed range endpoint on HEAD", file=sys.stderr)
        return 1
    if _range_start_on_endpoint(git, start, args.sha) is None:
        print("Error: completed notes start is not an ancestor of endpoint", file=sys.stderr)
        return 1
    try:
        if not {"last_tag", "tag_sha", "saved_marker_sha256", "baseline"}.issubset(receipt):
            raise ValueError("completed notes receipt lacks baseline evidence")
        marker_digest, tag_sha, baseline = _live_baseline(args.branch, receipt["last_tag"], args.sha, git)
        if (
            receipt["last_tag"] != args.last_tag
            or receipt["tag_sha"] != tag_sha
            or receipt["saved_marker_sha256"] != marker_digest
            or receipt["baseline"] != baseline
            or (baseline and _range_start_on_endpoint(git, start, baseline) is None)
        ):
            raise ValueError("completed notes baseline changed or range skips it")
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    path = _marker_path(args.branch, args.marker_dir)
    if any(component.is_symlink() for component in (path, *path.parents)):
        print("Error: release marker path must not traverse a symlink", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8", newline="\n") as marker:
            marker.write(args.sha.strip() + "\n")
    except OSError as exc:
        print(f"Error: release marker write failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the requested subcommand.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code (always 0; argparse exits 2 on bad/missing args).
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(
        prog="release_append_marker.py",
        description="Persist and resolve the /oss:release notes --append baseline marker.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_valid = sub.add_parser("is-valid", help="Print true/false for whether a usable, non-superseded marker exists.")
    p_valid.add_argument("--branch", required=True)
    p_valid.add_argument("--last-tag", required=True)
    p_valid.add_argument("--marker-dir", default=None)

    p_resolve = sub.add_parser("resolve", help="Print the --append RANGE (marker..HEAD or last-tag..HEAD).")
    p_resolve.add_argument("--branch", required=True)
    p_resolve.add_argument("--last-tag", required=True)
    p_resolve.add_argument("--marker-dir", default=None)

    p_receipt = sub.add_parser("receipt", help="Bind the completed notes range to current source and draft bytes.")
    p_receipt.add_argument("--branch", required=True)
    p_receipt.add_argument("--range-file", required=True)
    p_receipt.add_argument("--draft", required=True)
    p_receipt.add_argument("--output", required=True)
    p_receipt.add_argument("--last-tag", default="")

    p_write = sub.add_parser("write", help="Persist the completed notes range endpoint as the marker.")
    p_write.add_argument("--branch", required=True)
    p_write.add_argument("--sha", required=True)
    p_write.add_argument("--receipt")
    p_write.add_argument("--draft")
    p_write.add_argument("--marker-dir", default=None)
    p_write.add_argument("--last-tag", default="")

    p_guard = sub.add_parser("guard", help="Refuse ambiguous or pending branch state before any notes write.")
    p_guard.add_argument("--branch", required=True)

    args = parser.parse_args(argv)
    try:
        refuse_legacy(Path.cwd(), args.branch)
        _refuse_linked_marker(args.branch, getattr(args, "marker_dir", None))
        base = Path(args.marker_dir) if getattr(args, "marker_dir", None) else Path(".temp")
        journal = base / "release-state-v2" / branch_state_key(args.branch) / "journal.json"
        if journal.exists() or journal.is_symlink():
            raise ValueError("pending append publication; recover before using marker")
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    if args.command == "guard":
        return 0
    if args.command == "is-valid":
        return cmd_is_valid(args)
    if args.command == "resolve":
        return cmd_resolve(args)
    if args.command == "receipt":
        return cmd_receipt(args)
    return cmd_write(args)


if __name__ == "__main__":
    sys.exit(main())
