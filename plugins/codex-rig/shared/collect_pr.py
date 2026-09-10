#!/usr/bin/env python3
"""Collect authoritative pull-request evidence and an optional verified local checkout.

## Purpose

Assemble the PR-specific metadata, review threads, patch, Git target/head evidence, and local checkout required before a
source review. The bundle makes the authoritative base repository and exact PR head explicit before any reviewer
inspects source.

## Scope

It orchestrates one PR evidence recipe and writes its artifact bundle; every GitHub read delegates to ``github_read.py``
and remote mutation is forbidden. With ``--checkout``, it additionally selects the matching local remote, fetches the
target/head evidence, and records checkout identity without using forced Git operations.

## Usage

Run ``python collect_pr.py --target <number-or-url> --out <directory> [--checkout]`` from code-review or code-remediate
PR mode. Reusing an output directory is supported because the collector removes its own prior evidence before starting a
new attempt.

## Used by

The PR code-review/remediation workflows and collector acceptance tests use this module; it is not a general issue or
discussion reader. Its review-thread GraphQL query is intentionally limited to the PR identified by the fetched
``pr.json`` payload.

## Outputs

It writes PR metadata, threads, diff/stat files, routing, target/head checks, optional checkout evidence, and classified
terminal failure markers. A successful bundle includes files such as ``pr.json``, ``review-threads.json``,
``diff.patch``, ``pr-routing.json``, and checkout identity records when checkout was requested.

## Failure

Missing tools, unsafe core GitHub reads, invalid PR identity, or checkout mismatch returns ``2`` and blocks source
review. Review-thread collection is supplemental: its failure is recorded and lowers downstream confidence without
blocking exact local source review. Each attempt clears prior collector artifacts before starting, then retains evidence
and diagnostics from the current attempt if a later core step fails.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse


# Keep this executable helper importable when pytest discovers it as a module.
SHARED_DIRECTORY = Path(__file__).resolve().parent
if str(SHARED_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SHARED_DIRECTORY))

from github_read import GitHubReadError, read_with_fallback, run_gh_read  # noqa: E402


MAX_OUTPUT_BYTES = 16 * 1024 * 1024
VALID_PR_STATES = frozenset({"OPEN", "MERGED", "CLOSED"})
GITHUB_HOST = "github.com"
PR_NUMBER_PATTERN = re.compile(r"[1-9][0-9]*")
GITHUB_PATH_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
GIT_OBJECT_ID_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
FALLBACK_UNAVAILABLE_EVIDENCE = (
    "github_provided_file_list",
    "mergeability",
    "review_decision",
    "reviews",
    "top_level_comments",
)
COLLECTOR_EVIDENCE_ARTIFACTS = (
    "comments.json",
    "checkout-state.json",
    "diff.patch",
    "diffstat.txt",
    "files.txt",
    "local-checkout.json",
    "numstat.txt",
    "online-review-summary.json",
    "pr-head-fetch.json",
    "pr-routing.json",
    "pr.json",
    "remote-selection-error.txt",
    "remote-selection.json",
    "remote.txt",
    "review-threads.raw.json",
    "review-threads-command-failure.json",
    "review-threads-error.txt",
    "review-threads.json",
    "reviews.json",
    "status.txt",
    "target-branch.json",
    "unresolved-review-threads.json",
    "untracked.txt",
    "worktree-preflight.json",
)
PR_FIELDS = (
    "number,title,body,url,author,baseRefName,baseRefOid,headRefName,headRefOid,"
    "headRepository,headRepositoryOwner,isCrossRepository,state,isDraft,"
    "reviewDecision,mergeable,comments,reviews,files,statusCheckRollup"
)
GRAPHQL_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line startLine originalLine originalStartLine diffSide
          comments(first: 100) {
            nodes { id author { login } body url path position originalPosition line originalLine diffHunk createdAt updatedAt }
          }
        }
      }
    }
  }
}
"""
RunCommand = Callable[..., subprocess.CompletedProcess[bytes]]


class PRTarget(NamedTuple):
    """Represent a validated GitHub pull-request identity."""

    owner: str
    repository: str
    number: int

    @property
    def url(self) -> str:
        """Return the canonical GitHub pull-request URL."""
        return f"https://{GITHUB_HOST}/{self.owner}/{self.repository}/pull/{self.number}"


class CollectionError(RuntimeError):
    """Carry one stable bounded collection failure code."""

    def __init__(self, code: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        """Initialize a stable code with optional credential-opaque metadata."""
        super().__init__(code)
        self.diagnostics = diagnostics


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the portable PR collector command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="Artifact directory")
    parser.add_argument("--target", default="", help="PR number, canonical GitHub URL, or empty for current branch")
    parser.add_argument("--checkout", action="store_true", help="Fetch and update the verified local PR checkout")
    parser.add_argument("--timeout-seconds", type=int, default=60, help="Per-command timeout")
    arguments = parser.parse_args(argv)
    if arguments.timeout_seconds < 1:
        parser.error("--timeout-seconds must be positive")
    return arguments


def _run(run: RunCommand, argv: list[str], timeout: int, label: str, *, input_bytes: bytes | None = None) -> bytes:
    """Run one argv-only command and return bounded stdout bytes."""
    if argv and argv[0] == "gh":
        if input_bytes is not None:
            raise CollectionError(f"unsafe-gh-command:{label}")
        try:
            return run_gh_read(run, argv, timeout=timeout, label=label)
        except GitHubReadError as error:
            raise CollectionError(str(error), diagnostics=error.diagnostics) from error
    try:
        completed = run(
            argv,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise CollectionError(f"command-timeout:{label}") from error
    except OSError as error:
        raise CollectionError(f"command-unavailable:{label}") from error
    if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
        raise CollectionError(f"command-output-oversized:{label}")
    if completed.returncode != 0:
        failure_class = "command-failed"
        raise CollectionError(
            f"{failure_class}:{label}",
            diagnostics={
                "exit_code": completed.returncode,
                "failure_class": failure_class,
                "label": label,
            },
        )
    return completed.stdout


def _parse_pr_target(value: str) -> PRTarget | None:
    """Parse a positive PR number or exact canonical GitHub PR URL."""
    target = value.strip()
    if not target:
        return None
    if PR_NUMBER_PATTERN.fullmatch(target):
        return PRTarget("", "", int(target))
    if "%" in target:
        raise CollectionError("unsafe-pr-target")
    try:
        parsed = urlparse(target)
        port = parsed.port
    except ValueError as error:
        raise CollectionError("unsafe-pr-target") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != GITHUB_HOST
        or port is not None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise CollectionError("unsafe-pr-target")
    parts = parsed.path.split("/")
    if (
        len(parts) != 5
        or parts[0]
        or parts[3] != "pull"
        or not GITHUB_PATH_COMPONENT_PATTERN.fullmatch(parts[1])
        or not GITHUB_PATH_COMPONENT_PATTERN.fullmatch(parts[2])
        or not PR_NUMBER_PATTERN.fullmatch(parts[4])
    ):
        raise CollectionError("unsafe-pr-target")
    return PRTarget(parts[1], parts[2], int(parts[4]))


def _github_remote_identity(url: str) -> tuple[str, str] | None:
    """Return one configured GitHub remote's normalized owner and repository."""
    value = url.strip()
    scp_match = re.fullmatch(r"(?:[^@/\s]+@)?github\.com:([^/\s:]+)/([^/\s]+)", value, flags=re.IGNORECASE)
    if scp_match:
        owner, repository = scp_match.groups()
    else:
        try:
            parsed = urlparse(value)
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme not in {"git", "https", "ssh"}
            or parsed.hostname != GITHUB_HOST
            or port is not None
            or parsed.query
            or parsed.fragment
            or parsed.password
            or (parsed.username and parsed.scheme != "ssh")
        ):
            return None
        parts = parsed.path.split("/")
        if len(parts) != 3 or parts[0]:
            return None
        owner, repository = parts[1:]
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not GITHUB_PATH_COMPONENT_PATTERN.fullmatch(owner) or not GITHUB_PATH_COMPONENT_PATTERN.fullmatch(repository):
        return None
    return owner, repository


def _configured_github_repositories(run: RunCommand, timeout: int) -> dict[str, tuple[str, str]]:
    """Return distinct GitHub repository identities configured as local remotes."""
    repositories: dict[str, tuple[str, str]] = {}
    remote_names = (
        _run(run, ["git", "remote"], timeout, "github-remote-list").decode("utf-8", errors="strict").splitlines()
    )
    for remote_name in remote_names:
        if not remote_name:
            continue
        urls = (
            _run(
                run,
                ["git", "remote", "get-url", "--all", remote_name],
                timeout,
                "github-remote-url",
            )
            .decode("utf-8", errors="strict")
            .splitlines()
        )
        for url in urls:
            identity = _github_remote_identity(url)
            if identity is not None:
                repositories.setdefault("/".join(part.casefold() for part in identity), identity)
    return repositories


def _fallback_target(target: PRTarget | None, run: RunCommand, timeout: int) -> PRTarget | None:
    """Bind a fallback target to configured GitHub remotes or decline fallback."""
    if target is None:
        return None
    repositories = _configured_github_repositories(run, timeout)
    if target.owner and target.repository:
        key = f"{target.owner.casefold()}/{target.repository.casefold()}"
        configured_identity = repositories.get(key)
        if configured_identity is None:
            raise CollectionError("missing-matching-git-remote-for-pr-base")
        return PRTarget(*configured_identity, target.number)
    if len(repositories) != 1:
        return None
    owner, repository = next(iter(repositories.values()))
    return PRTarget(owner, repository, target.number)


def _read_pr_metadata(
    run: RunCommand,
    target: PRTarget | None,
    timeout: int,
    *,
    checkout: bool,
) -> tuple[bytes, str]:
    """Read PR metadata through gh with a trusted public REST fallback."""
    pr_args = [target.url if target and target.owner else str(target.number)] if target else []

    def fallback_url() -> str | None:
        """Resolve trusted repository identity only after an eligible primary failure."""
        if not checkout:
            return None
        fallback_target = _fallback_target(target, run, timeout)
        if fallback_target is None:
            return None
        return (
            f"https://api.github.com/repos/{fallback_target.owner}/{fallback_target.repository}"
            f"/pulls/{fallback_target.number}"
        )

    try:
        return read_with_fallback(
            run,
            ["gh", "pr", "view", *pr_args, "--json", PR_FIELDS],
            timeout=timeout,
            label="gh-pr-view",
            fallback_url=fallback_url,
        )
    except GitHubReadError as error:
        raise CollectionError(str(error), diagnostics=error.diagnostics) from error


def _normalized_public_pr(payload: dict[str, Any], target: PRTarget) -> dict[str, Any]:
    """Map one validated public REST PR response to the collector metadata schema."""
    base = payload.get("base")
    head = payload.get("head")
    user = payload.get("user")
    if not isinstance(base, dict) or not isinstance(head, dict) or not isinstance(user, dict):
        raise CollectionError("invalid-json:public-pr-metadata")
    base_repo = base.get("repo")
    head_repo = head.get("repo")
    if not isinstance(base_repo, dict) or not isinstance(payload.get("number"), int):
        raise CollectionError("invalid-json:public-pr-metadata")
    expected_base = f"{target.owner}/{target.repository}"
    if payload["number"] != target.number or not isinstance(base_repo.get("full_name"), str):
        raise CollectionError("public-pr-identity-mismatch")
    if base_repo["full_name"].casefold() != expected_base.casefold():
        raise CollectionError("public-pr-base-repository-mismatch")
    html_url = payload.get("html_url")
    try:
        returned_target = _parse_pr_target(html_url) if isinstance(html_url, str) else None
    except CollectionError as error:
        raise CollectionError("public-pr-identity-mismatch") from error
    if returned_target != target:
        raise CollectionError("public-pr-identity-mismatch")
    body = payload.get("body")
    if body is None:
        body = ""
    required_text = {
        "title": payload.get("title"),
        "author": user.get("login"),
        "base_ref": base.get("ref"),
        "base_oid": base.get("sha"),
        "head_ref": head.get("ref"),
        "head_oid": head.get("sha"),
    }
    if not isinstance(body, str) or not all(isinstance(value, str) and value for value in required_text.values()):
        raise CollectionError("invalid-json:public-pr-metadata")
    if not GIT_OBJECT_ID_PATTERN.fullmatch(required_text["base_oid"]) or not GIT_OBJECT_ID_PATTERN.fullmatch(
        required_text["head_oid"]
    ):
        raise CollectionError("invalid-json:public-pr-metadata")
    state = payload.get("state")
    if not isinstance(state, str) or state.upper() not in VALID_PR_STATES or not isinstance(payload.get("draft"), bool):
        raise CollectionError("invalid-json:public-pr-metadata")
    head_repository = head_repo.get("full_name") if isinstance(head_repo, dict) else ""
    if not isinstance(head_repository, str):
        raise CollectionError("invalid-json:public-pr-metadata")
    head_owner = head_repo.get("owner") if isinstance(head_repo, dict) else None
    head_repository_owner = head_owner.get("login") if isinstance(head_owner, dict) else ""
    if not isinstance(head_repository_owner, str):
        raise CollectionError("invalid-json:public-pr-metadata")
    return {
        "number": target.number,
        "title": required_text["title"],
        "body": body,
        "url": target.url,
        "author": {"login": required_text["author"]},
        "baseRefName": required_text["base_ref"],
        "baseRefOid": required_text["base_oid"],
        "headRefName": required_text["head_ref"],
        "headRefOid": required_text["head_oid"],
        "headRepository": {"nameWithOwner": head_repository},
        "headRepositoryOwner": {"login": head_repository_owner},
        "isCrossRepository": head_repository.casefold() != expected_base.casefold(),
        "state": state.upper(),
        "isDraft": payload["draft"],
        "reviewDecision": None,
        "mergeable": None,
        "comments": [],
        "reviews": [],
        "files": [],
        "statusCheckRollup": None,
    }


def _json(payload: bytes, label: str) -> dict[str, Any]:
    """Decode one bounded UTF-8 JSON object."""
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CollectionError(f"invalid-json:{label}") from error
    if not isinstance(value, dict):
        raise CollectionError(f"invalid-json:{label}")
    return value


def _write_json(path: Path, value: object) -> None:
    """Write one deterministic human-readable JSON artifact."""
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _git_path_list(payload: bytes) -> list[str]:
    """Decode and sort the NUL-delimited paths emitted by Git."""
    try:
        paths = [path for path in payload.decode("utf-8", errors="strict").split("\0") if path]
    except UnicodeDecodeError as error:
        raise CollectionError("invalid-utf8:git-path-list") from error
    return sorted(set(paths))


def _derived_diff_output(
    run: RunCommand,
    argv: list[str],
    timeout: int,
    label: str,
    diff: bytes,
) -> bytes:
    """Return derived diff output or record its non-applicable command failure."""
    try:
        return _run(run, argv, timeout, label, input_bytes=diff)
    except CollectionError as error:
        if str(error) != f"command-failed:{label}":
            raise
        return f"unavailable:{error}\n".encode()


def _optional_command_output(run: RunCommand, argv: list[str], timeout: int, label: str) -> bytes:
    """Return derived command output while keeping an unsupported statistic non-terminal."""
    try:
        return _run(run, argv, timeout, label)
    except CollectionError as error:
        if str(error) != f"command-failed:{label}":
            raise
        return f"unavailable:{error}\n".encode()


def _git_is_ancestor(run: RunCommand, timeout: int, ancestor: str, descendant: str) -> bool:
    """Return whether one verified Git commit is an ancestor of another."""
    argv = ["git", "merge-base", "--is-ancestor", ancestor, descendant]
    try:
        completed = run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise CollectionError("command-timeout:target-branch-ancestry") from error
    except OSError as error:
        raise CollectionError("command-unavailable:target-branch-ancestry") from error
    if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
        raise CollectionError("command-output-oversized:target-branch-ancestry")
    if completed.returncode in {0, 1}:
        return completed.returncode == 0
    raise CollectionError(
        "command-failed:target-branch-ancestry",
        diagnostics={
            "exit_code": completed.returncode,
            "failure_class": "command-failed",
            "label": "target-branch-ancestry",
        },
    )


def _head_repository(payload: dict[str, Any]) -> str:
    """Normalize GitHub's head-repository object variants."""
    value = payload.get("headRepository")
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    for key in ("nameWithOwner", "name_with_owner"):
        candidate = value.get(key)
        if isinstance(candidate, str) and "/" in candidate:
            return candidate
    name = value.get("name")
    owner: object = value.get("owner", payload.get("headRepositoryOwner"))
    if isinstance(owner, dict):
        owner = owner.get("login") or owner.get("name")
    return f"{owner}/{name}" if isinstance(owner, str) and isinstance(name, str) else ""


def _review_threads(thread_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one complete normalized review-thread page."""
    try:
        container = thread_payload["data"]["repository"]["pullRequest"]["reviewThreads"]
        threads = container.get("nodes") or []
        page_info = container.get("pageInfo") or {}
    except (KeyError, TypeError) as error:
        raise CollectionError("invalid-json:review-threads") from error
    if not isinstance(threads, list) or not isinstance(page_info, dict):
        raise CollectionError("invalid-json:review-threads")
    if page_info.get("hasNextPage"):
        raise CollectionError("review-thread-pagination-incomplete")
    return [item for item in threads if isinstance(item, dict)]


def _review_artifacts(
    payload: dict[str, Any],
    threads: list[dict[str, Any]],
    output: Path,
    *,
    base_repo: str,
    base_host: str,
    thread_error: str | None,
    pr_metadata_transport: str,
) -> dict[str, Any]:
    """Validate core metadata and emit normalized online-review artifacts."""
    comments = payload.get("comments") or []
    reviews = payload.get("reviews") or []
    files = payload.get("files") or []
    if not isinstance(comments, list) or not isinstance(reviews, list) or not isinstance(files, list):
        raise CollectionError("invalid-json:pr-view")
    if not isinstance(payload.get("body"), str):
        raise CollectionError("missing-pr-description")
    file_names = sorted(item["path"] for item in files if isinstance(item, dict) and isinstance(item.get("path"), str))
    unresolved = [item for item in threads if isinstance(item, dict) and item.get("isResolved") is False]
    active = [item for item in unresolved if item.get("isOutdated") is not True]
    outdated = [item for item in unresolved if item.get("isOutdated") is True]
    summary = {
        "pr_metadata_transport": pr_metadata_transport,
        "limited_data": pr_metadata_transport == "public-https-fallback",
        "unavailable_evidence": list(FALLBACK_UNAVAILABLE_EVIDENCE)
        if pr_metadata_transport == "public-https-fallback"
        else [],
        "review_threads_status": "unavailable" if thread_error else "available",
        "review_threads_error": thread_error,
        "review_thread_count": len(threads),
        "unresolved_review_thread_count": len(unresolved),
        "active_unresolved_review_thread_count": len(active),
        "outdated_unresolved_review_thread_count": len(outdated),
        "top_level_comment_count": len(comments),
        "review_count": len(reviews),
    }
    pr_state = payload.get("state")
    if pr_state not in VALID_PR_STATES:
        raise CollectionError("unsupported-pr-state")
    head_repo = _head_repository(payload)
    routing = {
        "base_repo": base_repo,
        "base_host": base_host,
        "base_identity_source": "pr_url",
        "pr_number": payload.get("number"),
        "pr_url": payload.get("url"),
        "pr_state": pr_state,
        "base_ref": payload.get("baseRefName"),
        "base_oid": payload.get("baseRefOid"),
        "head_ref": payload.get("headRefName"),
        "head_oid": payload.get("headRefOid"),
        "head_repo": head_repo,
        "is_cross_repository": bool(payload.get("isCrossRepository")),
        "same_repo": bool(head_repo and base_repo and head_repo.casefold() == base_repo.casefold()),
        "local_checkout_required": True,
        "local_checkout_command": f"gh pr checkout {payload.get('number')}",
        "force_policy": "never pass --force to git or gh automatically; stop and ask the user first",
        "source_policy": (
            "inspect the exact local checkout and derive its diff locally; use public REST metadata with unavailable "
            "review evidence marked explicitly"
            if pr_metadata_transport == "public-https-fallback"
            else "inspect the exact local checkout and derive its diff locally; use gh for PR metadata and supplemental review evidence"
        ),
        "pr_metadata_transport": pr_metadata_transport,
    }
    (output / "files.txt").write_text("".join(f"{name}\n" for name in file_names), encoding="utf-8")
    _write_json(output / "comments.json", comments)
    _write_json(output / "reviews.json", reviews)
    _write_json(output / "review-threads.json", threads)
    _write_json(output / "unresolved-review-threads.json", unresolved)
    _write_json(output / "online-review-summary.json", summary)
    _write_json(output / "pr-routing.json", routing)
    return routing


def _selector(
    run: RunCommand,
    timeout: int,
    script: Path,
    url: str,
    *,
    identity_only: bool,
) -> dict[str, Any]:
    """Resolve repository identity or matching local remote through the packaged selector."""
    argv = [sys.executable, str(script), "--expected-url", url]
    if identity_only:
        argv.append("--identity-only")
    return _json(_run(run, argv, timeout, "select-git-remote"), "select-git-remote")


def _checkout(
    run: RunCommand,
    timeout: int,
    output: Path,
    payload: dict[str, Any],
    routing: dict[str, Any],
    selector: Path,
) -> dict[str, Any]:
    """Fetch verified target/head refs and update the local PR checkout without force."""
    url = routing.get("pr_url")
    number = routing.get("pr_number")
    base_ref = routing.get("base_ref")
    base_oid = routing.get("base_oid")
    head_ref = routing.get("head_ref")
    head_oid = routing.get("head_oid")
    if not all(isinstance(value, str) and value for value in (url, base_ref, base_oid, head_oid)):
        raise CollectionError("missing-pr-checkout-identity")
    remote = _selector(run, timeout, selector, url, identity_only=False)
    remote_name = remote.get("remote")
    remote_url = remote.get("remote_url")
    if not isinstance(remote_name, str) or not isinstance(remote_url, str):
        raise CollectionError("missing-matching-git-remote-for-pr-base")
    _write_json(output / "remote-selection.json", remote)
    (output / "remote-selection-error.txt").write_bytes(b"")
    (output / "remote.txt").write_text(f"{remote_name} {remote_url}\n", encoding="utf-8")

    base_remote_ref = f"refs/remotes/{remote_name}/{base_ref}"
    _run(
        run,
        ["git", "fetch", "--no-tags", remote_name, f"{base_ref}:{base_remote_ref}"],
        timeout,
        "target-branch-fetch",
    )
    base_local = _run(run, ["git", "rev-parse", base_remote_ref], timeout, "target-branch-rev-parse").decode().strip()
    base_matches = base_local == base_oid
    base_is_ancestor = base_matches or _git_is_ancestor(run, timeout, base_oid, base_local)
    base_relation = "matches-pr-metadata" if base_matches else "advanced" if base_is_ancestor else "diverged"
    target = {
        "status": "fetched",
        "remote": remote_name,
        "remote_url": remote_url,
        "base_ref": base_ref,
        "remote_ref": base_remote_ref,
        "local_head": base_local,
        "expected_base_oid": base_oid,
        "base_matches_pr_metadata": base_matches,
        "expected_base_is_ancestor": base_is_ancestor,
        "base_relation": base_relation,
        "command": f"git fetch --no-tags {remote_name} {base_ref}:{base_remote_ref}",
        "source_policy": "target branch is refreshed before review; advancement from the PR-recorded base is review context, while divergence fails an open-PR review",
    }
    _write_json(output / "target-branch.json", target)
    if routing.get("pr_state") == "OPEN" and not base_is_ancestor:
        raise CollectionError(f"target-branch-diverged:{base_local}:{base_oid}")

    use_pull_ref = routing.get("pr_metadata_transport") == "public-https-fallback" and isinstance(number, int)
    if use_pull_ref:
        head_remote_ref = f"refs/remotes/{remote_name}/pull/{number}/head"
        _run(
            run,
            ["git", "fetch", "--no-tags", remote_name, f"refs/pull/{number}/head:{head_remote_ref}"],
            timeout,
            "public-pr-head-fetch",
        )
        head_local = (
            _run(run, ["git", "rev-parse", head_remote_ref], timeout, "public-pr-head-rev-parse").decode().strip()
        )
        head = {
            "status": "fetched",
            "remote": remote_name,
            "head_ref": head_remote_ref,
            "local_head": head_local,
            "expected_head_oid": head_oid,
            "head_matches_pr_metadata": head_local == head_oid,
            "command": f"git fetch --no-tags {remote_name} refs/pull/{number}/head:{head_remote_ref}",
            "source_policy": "public fallback verifies GitHub's pull ref against REST metadata before detached local checkout",
        }
        _write_json(output / "pr-head-fetch.json", head)
        if head_local != head_oid:
            raise CollectionError(f"public-pr-head-oid-mismatch:{head_local}:{head_oid}")
    elif (
        routing.get("same_repo") is True
        and routing.get("pr_state") == "OPEN"
        and isinstance(head_ref, str)
        and head_ref
    ):
        head_remote_ref = f"refs/remotes/{remote_name}/{head_ref}"
        _run(
            run,
            ["git", "fetch", "--no-tags", remote_name, f"{head_ref}:{head_remote_ref}"],
            timeout,
            "pr-head-fetch",
        )
        head_local = _run(run, ["git", "rev-parse", head_remote_ref], timeout, "pr-head-rev-parse").decode().strip()
        head = {
            "status": "fetched",
            "remote": remote_name,
            "head_ref": head_ref,
            "remote_ref": head_remote_ref,
            "local_head": head_local,
            "expected_head_oid": head_oid,
            "head_matches_pr_metadata": head_local == head_oid,
            "command": f"git fetch --no-tags {remote_name} {head_ref}:{head_remote_ref}",
            "source_policy": "PR branch is refreshed before local checkout and conflict analysis",
        }
        _write_json(output / "pr-head-fetch.json", head)
        if head_local != head_oid:
            raise CollectionError(f"pr-head-oid-mismatch:{head_local}:{head_oid}")
    elif isinstance(number, int):
        # Fork commits may be absent locally; fetch before inspecting checkout overlap, not during checkout.
        pull_head_ref = f"refs/remotes/{remote_name}/pull/{number}/head"
        historical = routing.get("pr_state") != "OPEN"
        head_label = "historical-pr-head" if historical else "pr-head"
        _run(
            run,
            ["git", "fetch", "--no-tags", remote_name, f"refs/pull/{number}/head:{pull_head_ref}"],
            timeout,
            f"{head_label}-fetch",
        )
        head_local = _run(run, ["git", "rev-parse", pull_head_ref], timeout, f"{head_label}-rev-parse").decode().strip()
        head = {
            "status": "fetched",
            "remote": remote_name,
            "head_ref": pull_head_ref,
            "local_head": head_local,
            "expected_head_oid": head_oid,
            "head_matches_pr_metadata": head_local == head_oid,
            "command": f"git fetch --no-tags {remote_name} refs/pull/{number}/head:{pull_head_ref}",
            "source_policy": "PR head is refreshed from GitHub's pull ref and verified against metadata before checkout preflight",
        }
        _write_json(output / "pr-head-fetch.json", head)
        if head_local != head_oid:
            raise CollectionError(f"{head_label}-oid-mismatch:{head_local}:{head_oid}")
    else:
        raise CollectionError("missing-pr-checkout-identity")

    current_head = _run(run, ["git", "rev-parse", "HEAD"], timeout, "pre-checkout-head").decode().strip()
    dirty_paths = _git_path_list(
        _run(run, ["git", "diff", "--name-only", "-z", "HEAD", "--"], timeout, "tracked-worktree-paths")
    )
    checkout_paths = []
    if current_head != head_oid:
        checkout_paths = _git_path_list(
            _run(
                run,
                ["git", "diff", "--name-only", "-z", current_head, head_oid, "--"],
                timeout,
                "checkout-paths",
            )
        )
    overlapping_paths = sorted(set(dirty_paths).intersection(checkout_paths))
    preflight_status = "clean"
    if current_head == head_oid:
        preflight_status = "already-at-pr-head"
    elif dirty_paths:
        preflight_status = "safe-unrelated-dirty-paths"
    if overlapping_paths:
        preflight_status = "blocked-overlapping-dirty-paths"
    preflight = {
        "status": preflight_status,
        "current_head": current_head,
        "expected_head": head_oid,
        "dirty_paths": dirty_paths,
        "checkout_paths": checkout_paths,
        "overlapping_paths": overlapping_paths,
    }
    _write_json(output / "worktree-preflight.json", preflight)
    if overlapping_paths:
        raise CollectionError("dirty-tracked-worktree-overlap-before-pr-checkout")
    checkout_argv = ["gh", "pr", "checkout", str(number)]
    if (use_pull_ref or routing.get("pr_state") != "OPEN") and isinstance(number, int):
        checkout_argv = ["git", "checkout", "--detach", f"refs/remotes/{remote_name}/pull/{number}/head"]
    routing["local_checkout_command"] = " ".join(checkout_argv)
    _write_json(output / "pr-routing.json", routing)
    checkout_command = "not-run: already at expected PR head"
    if current_head != head_oid:
        # gh may alter refs or the worktree before returning an error; retain conservative state first.
        _write_json(
            output / "checkout-state.json",
            {"status": "checkout-command-started", "local_state": "changed-or-unknown"},
        )
        _run(run, checkout_argv, timeout, "local-pr-checkout")
        checkout_command = " ".join(checkout_argv)
        _write_json(
            output / "checkout-state.json",
            {"status": "checkout-command-succeeded-unverified", "local_state": "changed-or-unknown"},
        )
    branch = _run(run, ["git", "branch", "--show-current"], timeout, "checkout-branch").decode().strip()
    local_head = _run(run, ["git", "rev-parse", "HEAD"], timeout, "checkout-head").decode().strip()
    matches = local_head == head_oid
    checkout_evidence = {
        "status": "checked-out",
        "pr_number": number,
        "pr_url": url,
        "local_branch": branch,
        "local_head": local_head,
        "expected_head": head_oid,
        "head_matches_pr": matches,
        "command": checkout_command,
        "target_branch_artifact": "target-branch.json",
        "pr_head_fetch_artifact": "pr-head-fetch.json",
        "diff_source": "verified-local-checkout",
        "diff_base_oid": base_oid,
        "diff_head_oid": head_oid,
        "diff_command": f"git diff --binary {base_oid}...{head_oid} --",
        "force_policy": "no --force was used; ask the user before any forced checkout",
        "source_policy": "local checkout is authoritative for code inspection and edits",
    }
    _write_json(output / "local-checkout.json", checkout_evidence)
    if not matches:
        raise CollectionError("local-checkout-head-mismatch")
    _write_json(
        output / "checkout-state.json",
        {"status": "checkout-verified", "local_state": "exact-pr-head", "local_head": local_head},
    )
    return checkout_evidence


def _clear_collector_artifacts(output: Path) -> None:
    """Remove this collector's prior evidence so one output directory never mixes attempts."""
    for filename in (*COLLECTOR_EVIDENCE_ARTIFACTS, "command-failure.json", "pr-error.txt"):
        (output / filename).unlink(missing_ok=True)


def collect_pr(
    *,
    target: str,
    output: Path,
    checkout: bool,
    timeout_seconds: int,
    run: RunCommand | None = None,
) -> int:
    """Collect one PR context pack and optionally update its verified checkout."""
    output.mkdir(parents=True, exist_ok=True)
    _clear_collector_artifacts(output)
    command_runner = subprocess.run if run is None else run
    try:
        requested_target = _parse_pr_target(target)
        normalized_target = (
            requested_target.url
            if requested_target and requested_target.owner
            else (str(requested_target.number) if requested_target else "current-branch-pr")
        )
        (output / "pr-target.txt").write_text(f"{normalized_target}\n", encoding="utf-8")
        for command in ("git", "gh"):
            if shutil.which(command) is None:
                raise CollectionError(f"missing-command:{command}")
        try:
            status = _run(command_runner, ["git", "status", "--short"], timeout_seconds, "git-status")
        except CollectionError:
            status = b""
        (output / "status.txt").write_bytes(status)
        selector = Path(__file__).resolve().with_name("select-git-remote.py")
        payload_bytes, pr_metadata_transport = _read_pr_metadata(
            command_runner,
            requested_target,
            timeout_seconds,
            checkout=checkout,
        )
        raw_payload = _json(payload_bytes, "pr-view")
        if pr_metadata_transport == "public-https-fallback":
            fallback_target = _fallback_target(requested_target, command_runner, timeout_seconds)
            if fallback_target is None:
                raise CollectionError("missing-trusted-pr-fallback-identity")
            if not checkout:
                raise CollectionError("public-pr-fallback-requires-checkout")
            payload = _normalized_public_pr(raw_payload, fallback_target)
            payload_bytes = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
        else:
            payload = raw_payload
        url = payload.get("url")
        number = payload.get("number")
        try:
            returned_target = _parse_pr_target(url) if isinstance(url, str) else None
        except CollectionError as error:
            raise CollectionError("missing-pr-identity") from error
        if returned_target is None or not isinstance(number, int) or returned_target.number != number:
            raise CollectionError("missing-pr-identity")
        if requested_target and requested_target.owner:
            requested_identity = (requested_target.owner.casefold(), requested_target.repository.casefold())
            returned_identity = (returned_target.owner.casefold(), returned_target.repository.casefold())
            if returned_target.number != requested_target.number or returned_identity != requested_identity:
                raise CollectionError("pr-identity-mismatch")
        (output / "pr.json").write_bytes(payload_bytes)
        base_repo = f"{returned_target.owner}/{returned_target.repository}"
        base_host = GITHUB_HOST
        owner, repository = returned_target.owner, returned_target.repository
        threads: list[dict[str, Any]] = []
        thread_error: str | None = None
        try:
            threads_bytes = _run(
                command_runner,
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    f"owner={owner}",
                    "-f",
                    f"name={repository}",
                    "-F",
                    f"number={number}",
                    "-f",
                    f"query={GRAPHQL_QUERY}",
                ],
                timeout_seconds,
                "gh-review-threads",
            )
            thread_payload = _json(threads_bytes, "review-threads")
            (output / "review-threads.raw.json").write_bytes(threads_bytes)
            threads = _review_threads(thread_payload)
        except CollectionError as error:
            thread_error = str(error)
            (output / "review-threads-error.txt").write_text(f"{thread_error}\n", encoding="utf-8")
            if error.diagnostics is not None:
                _write_json(output / "review-threads-command-failure.json", error.diagnostics)
        routing = _review_artifacts(
            payload,
            threads,
            output,
            base_repo=base_repo,
            base_host=base_host,
            thread_error=thread_error,
            pr_metadata_transport=pr_metadata_transport,
        )
        (output / "untracked.txt").write_bytes(b"")
        if checkout:
            checkout_evidence = _checkout(command_runner, timeout_seconds, output, payload, routing, selector)
            revision_range = f"{checkout_evidence['diff_base_oid']}...{checkout_evidence['diff_head_oid']}"
            diff = _run(
                command_runner,
                ["git", "diff", "--binary", revision_range, "--"],
                timeout_seconds,
                "local-pr-diff",
            )
            (output / "diff.patch").write_bytes(diff)
            (output / "diffstat.txt").write_bytes(
                _optional_command_output(
                    command_runner,
                    ["git", "diff", "--stat", revision_range, "--"],
                    timeout_seconds,
                    "diff-stat",
                )
            )
            (output / "numstat.txt").write_bytes(
                _optional_command_output(
                    command_runner,
                    ["git", "diff", "--numstat", revision_range, "--"],
                    timeout_seconds,
                    "diff-numstat",
                )
            )
        else:
            pr_args = [returned_target.url]
            diff = _run(command_runner, ["gh", "pr", "diff", *pr_args], timeout_seconds, "gh-pr-diff")
            (output / "diff.patch").write_bytes(diff)
            (output / "diffstat.txt").write_bytes(
                _derived_diff_output(
                    command_runner,
                    ["git", "apply", "--stat"],
                    timeout_seconds,
                    "diff-stat",
                    diff,
                )
            )
            (output / "numstat.txt").write_bytes(
                _derived_diff_output(
                    command_runner,
                    ["git", "apply", "--numstat"],
                    timeout_seconds,
                    "diff-numstat",
                    diff,
                )
            )
        return 0
    except CollectionError as error:
        # Prior-attempt evidence was cleared at entry; retain this attempt for recovery and diagnosis.
        if error.diagnostics is not None:
            _write_json(output / "command-failure.json", error.diagnostics)
        (output / "pr-error.txt").write_text(f"{error}\n", encoding="utf-8")
        return 2


def main(argv: list[str] | None = None) -> int:
    """Run the command-line collector."""
    arguments = parse_args(argv)
    return collect_pr(
        target=arguments.target,
        output=arguments.out,
        checkout=arguments.checkout,
        timeout_seconds=arguments.timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
