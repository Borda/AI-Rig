"""Verify live GitHub identity and permissions before offering PR worktree publication.

## Purpose

Bind an open pull request and its exact destination ref to the admitted routing with a fixed gh field list. Check the
viewer's repository permissions before the workflow asks for push consent.

## Scope

Use the credential-opaque GitHub reader for every network read. Local delivery, ancestry, worktree ownership,
credential-helper collection and approval remain the publication gate's responsibility.

## Usage

Run ``publication_preflight.py --routing <pr-routing.json>`` or call ``publication_preflight(routing)`` with the decoded
collector record. An optional ``--fields`` projection selects verified PR fields to return, while the live query always
reads the full allowlist. Pass ``--retry`` only when repeating a verdict that permitted a retry.

## Outputs

Print a single compact JSON verdict. Success includes the verified PR fields, repository identities, ref and OID. A
refusal includes its safe reason, stage, next action and retry decision, with no partial verification data.

## Failure

The CLI returns 2 for a refused preflight. Network and rate-limit failures permit one explicit retry. Invalid or changed
evidence, permission failures and exhausted retries leave delivery local. No raw failure output is emitted.

## Used by

The Code Remediate PR worktree publication gate before its native question and again before consuming approval.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from github_read import DEFAULT_RUN_COMMAND, MAX_OUTPUT_BYTES, GitHubReadError, run_gh_read

PR_FIELDS = (
    "state",
    "baseRefName",
    "headRefName",
    "headRepository",
    "headRepositoryOwner",
    "headRefOid",
    "isCrossRepository",
    "url",
    "number",
    "title",
    "baseRefOid",
)
REPOSITORY = r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.{1,2}(?:/|$))[A-Za-z0-9_.-]+"
PR_URL = re.compile(rf"https://github\.com/(?P<repo>{REPOSITORY})/pull/(?P<number>[1-9][0-9]*)")
OID = re.compile(r"[0-9a-f]{40}")
BRANCH_NAME = re.compile(r"[^\x00-\x20\x7f\ud800-\udfff]+")
WRITE_PERMISSIONS = ("WRITE", "MAINTAIN", "ADMIN")
LOCAL_ACTION = "Keep delivery local and report the reason without offering publication."
NETWORK_ACTION = "Check the classified network condition and retry once through this helper."
RETRY_ACTIONS = {
    "dns": NETWORK_ACTION,
    "connection-refused": NETWORK_ACTION,
    "connection-reset": NETWORK_ACTION,
    "timeout": NETWORK_ACTION,
    "tls": NETWORK_ACTION,
    "network": NETWORK_ACTION,
    "rate-limit": "Check the rate-limit window and retry once through this helper.",
}


class _PreflightFailure(ValueError):
    """Carry a classified refusal with a safe read stage and optional recovery action."""

    def __init__(self, reason: str, stage: str, retry_action: str | None = None) -> None:
        """Retain only known codes and workflow actions."""
        super().__init__(reason)
        self.stage = stage
        self.retry_action = retry_action


def _json_object(payload: bytes, stage: str) -> dict[str, Any]:
    """Decode a bounded reader payload as a single JSON object."""
    try:
        value = json.loads(payload)
    except (ValueError, RecursionError) as error:
        raise _PreflightFailure("invalid-json", stage) from error
    if not isinstance(value, dict):
        raise _PreflightFailure("invalid-json", stage)
    return value


def _read_object(args: list[str], stage: str, retried: bool) -> dict[str, Any]:
    """Run the audited reader and expose only the classified retry action on failure."""
    try:
        payload = run_gh_read(DEFAULT_RUN_COMMAND, ["gh", *args], timeout=60, label=stage)
    except GitHubReadError as error:
        reason = str((error.diagnostics or {}).get("failure_reason", str(error).partition(":")[0]))
        action = None if retried else RETRY_ACTIONS.get(reason)
        raise _PreflightFailure(reason, stage, action) from error
    return _json_object(payload, stage)


def _read_api(path: str, stage: str, retried: bool) -> dict[str, Any]:
    """Pin REST reads to the same github.com identity as the canonical PR URL."""
    return _read_object(["api", path, "--hostname", "github.com"], stage, retried)


def _routing_identity(routing: dict[str, Any]) -> tuple[str, str, str]:
    """Validate admitted repository coordinates before using them in an audited command."""
    url, base_repo, head_repo = (routing.get(field) for field in ("pr_url", "base_repo", "head_repo"))
    if not all(isinstance(value, str) for value in (url, base_repo, head_repo)):
        raise _PreflightFailure("invalid-publication-routing", "routing")
    target = PR_URL.fullmatch(url)
    if not target or target["repo"].casefold() != base_repo.casefold() or not re.fullmatch(REPOSITORY, head_repo):
        raise _PreflightFailure("invalid-publication-routing", "routing")
    number = routing.get("pr_number")
    if type(number) is not int or str(number) != target["number"]:
        raise _PreflightFailure("invalid-publication-routing", "routing")
    for field, pattern in (("head_oid", OID), ("base_ref", BRANCH_NAME), ("head_ref", BRANCH_NAME)):
        value = routing.get(field)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise _PreflightFailure("invalid-publication-routing", "routing")
    # Ref components are used in a REST path. The delivery gate separately validates Git ref syntax.
    if {"", ".", ".."}.intersection(routing["head_ref"].split("/")):
        raise _PreflightFailure("invalid-publication-routing", "routing")
    return url, base_repo, head_repo


def _head_identity(pr: dict[str, Any]) -> str:
    """Read the owner and repository name from gh's exported PR objects."""
    repo, owner = pr.get("headRepository"), pr.get("headRepositoryOwner")
    if not isinstance(repo, dict) or not isinstance(owner, dict):
        return ""
    name, login = repo.get("name"), owner.get("login")
    return f"{login}/{name}" if isinstance(name, str) and isinstance(login, str) else ""


def _verify_pr(pr: dict[str, Any], routing: dict[str, Any], url: str, head_repo: str, cross_repo: bool) -> None:
    """Require a complete open PR whose destination and routing still match admission."""
    if not set(PR_FIELDS).issubset(pr):
        raise _PreflightFailure("incomplete-publication-pr", "pr-view")
    if pr["state"] != "OPEN":
        raise _PreflightFailure("publication-pr-not-open", "pr-view")
    if not isinstance(pr["title"], str) or not isinstance(pr["baseRefOid"], str) or not OID.fullmatch(pr["baseRefOid"]):
        raise _PreflightFailure("incomplete-publication-pr", "pr-view")
    expected = {
        "number": routing["pr_number"],
        "baseRefName": routing["base_ref"],
        "headRefName": routing["head_ref"],
        "headRefOid": routing["head_oid"],
    }
    if (
        not isinstance(pr["url"], str)
        or pr["url"].casefold() != url.casefold()
        or type(pr["number"]) is not int
        or any(pr[field] != value for field, value in expected.items())
        or _head_identity(pr).casefold() != head_repo.casefold()
        or pr["isCrossRepository"] is not cross_repo
    ):
        raise _PreflightFailure("publication-pr-changed", "pr-view")


def _verify_ref(payload: dict[str, Any], ref: str, oid: str) -> None:
    """Require the one requested branch ref to resolve to the admitted commit."""
    target = payload.get("object")
    if (
        payload.get("ref") != ref
        or not isinstance(target, dict)
        or target.get("type") != "commit"
        or target.get("sha") != oid
    ):
        raise _PreflightFailure("publication-ref-mismatch", "head-ref")


def _can_write(repository: str, stage: str, retried: bool) -> bool:
    """Check the authenticated viewer's push permission for one verified repository."""
    permission = _read_object(
        ["repo", "view", f"github.com/{repository}", "--json", "viewerPermission"], stage, retried
    )
    return permission.get("viewerPermission") in WRITE_PERMISSIONS


def _verify_permission(
    routing: dict[str, Any], base_repo: str, head_repo: str, cross_repo: bool, retried: bool
) -> None:
    """Preserve head-repository write access and the conditional fork-maintainer route."""
    if _can_write(head_repo, "head-permission", retried):
        return
    if cross_repo:
        path = f"/repos/{base_repo}/pulls/{routing['pr_number']}"
        permissions = _read_api(path, "fork-permission", retried)
        if permissions.get("maintainer_can_modify") is True and _can_write(base_repo, "base-permission", retried):
            return
    raise _PreflightFailure("publication-permission-unverified", "push-permission")


def _failed(error: _PreflightFailure) -> dict[str, Any]:
    """Report an ineligible result with no offer fields and no raw reader output."""
    return {
        "status": "failed",
        "reason": str(error),
        "stage": error.stage,
        "retry": error.retry_action is not None,
        "next_action": error.retry_action or LOCAL_ACTION,
    }


def publication_preflight(
    routing: dict[str, Any], *, fields: tuple[str, ...] = PR_FIELDS, retried: bool = False
) -> dict[str, Any]:
    """Read and verify current PR publication evidence against admitted routing."""
    try:
        if not fields or set(fields).difference(PR_FIELDS):
            raise _PreflightFailure("unknown-json-field", "requested-fields")
        url, base_repo, head_repo = _routing_identity(routing)
        cross_repo = base_repo.casefold() != head_repo.casefold()
        pr = _read_object(["pr", "view", url, "--json", ",".join(PR_FIELDS)], "pr-view", retried)
        _verify_pr(pr, routing, url, head_repo, cross_repo)
        ref = f"refs/heads/{routing['head_ref']}"
        path = f"/repos/{head_repo}/git/ref/{quote(ref.removeprefix('refs/'), safe='/')}"
        _verify_ref(_read_api(path, "head-ref", retried), ref, routing["head_oid"])
        _verify_permission(routing, base_repo, head_repo, cross_repo, retried)
    except _PreflightFailure as error:
        return _failed(error)
    return {
        "status": "ok",
        "retry": False,
        "pr": {field: pr[field] for field in fields},
        "base_repo": base_repo,
        "head_repo": head_repo,
        "ref": ref,
        "remote_head_oid": routing["head_oid"],
    }


def main(argv: list[str] | None = None) -> int:
    """Read the admitted routing record and print exactly one preflight verdict."""
    parser = argparse.ArgumentParser(description="Verify GitHub evidence for the PR publication gate.")
    parser.add_argument("--routing", type=Path, required=True, help="Admitted pr-routing.json record")
    parser.add_argument("--fields", help="Optional comma-separated projection of supported PR fields")
    parser.add_argument("--retry", action="store_true", help="Spend the one retry permitted by the previous verdict")
    args = parser.parse_args(argv)
    try:
        with args.routing.open("rb") as stream:
            payload = stream.read(MAX_OUTPUT_BYTES + 1)
        if len(payload) > MAX_OUTPUT_BYTES:
            raise _PreflightFailure("invalid-publication-routing", "routing")
        routing = _json_object(payload, "routing")
        fields = PR_FIELDS if args.fields is None else tuple(args.fields.split(","))
        verdict = publication_preflight(routing, fields=fields, retried=args.retry)
    except _PreflightFailure as error:
        verdict = _failed(error)
    except OSError:
        verdict = _failed(_PreflightFailure("invalid-publication-routing", "routing"))
    print(json.dumps(verdict, separators=(",", ":")))
    return 0 if verdict["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
