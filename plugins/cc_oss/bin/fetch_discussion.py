#!/usr/bin/env python
"""Purpose: Fetch a GitHub discussion and merge both levels of its conversation deterministically.

Scope: Read-only GitHub GraphQL requests through ``gh``, with no shell interpolation. Top-level comments and replies are
deduplicated by id. At most 200 comments and 200 replies across all parents are retained; initial nested reply pages
consume that same reply budget. Conversation text remains untrusted data and is never executed.

Usage: ``fetch_discussion.py --repo OWNER/REPO --number N --output PATH``.

Outputs: A merged discussion JSON object at PATH, with ``_fetch`` status, retained counts and errors. Stdout contains
one bounded JSON summary without conversation bodies or upstream stderr. The private sibling writer replaces stale
output without following a symlink at the final path component.

Failure: Exit 1 for failed, malformed, cyclic or capped pagination, retaining available evidence with an explicit
``incomplete`` status. Exit 0 means every requested conversation page completed. Invalid arguments exit 2; an output
write failure exits 1 and prints an error to stderr. No network retry or fallback conceals missing evidence.

Used by: ``skills/analyse/modes/thread.md`` on discussion cache misses. Offline regressions mock only subprocess.run.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from _sentinel_io import write_private_text  # noqa: E402

_CAP = 200
_DISCUSSION_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    discussion(number: $number) {
      title body createdAt closed closedAt
      author { login }
      category { name }
      answer { body author { login } createdAt }
      labels(first: 10) { nodes { name } }
      comments(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id body author { login } createdAt
          replies(first: 50) {
            pageInfo { hasNextPage endCursor }
            nodes { id body author { login } createdAt }
          }
        }
      }
    }
  }
}
"""
_REPLIES_QUERY = """
query($commentId: ID!, $after: String) {
  node(id: $commentId) {
    ... on DiscussionComment {
      id
      replies(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id body author { login } createdAt }
      }
    }
  }
}
"""


def _request(query: str, variables: dict[str, str | int], errors: list[str], label: str) -> dict[str, Any]:
    """Request one page and preserve partial GraphQL data while recording every transport failure."""
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        command.extend(["-F" if isinstance(value, int) else "-f", f"{key}={value}"])
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=15)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        errors.append(f"{label}: request unavailable or timed out")
        return {}
    if result.returncode != 0:
        errors.append(f"{label}: gh request failed")
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        errors.append(f"{label}: invalid JSON response")
        return {}
    if not isinstance(payload, dict):
        errors.append(f"{label}: invalid GraphQL response")
        return {}
    if payload.get("errors"):
        errors.append(f"{label}: GraphQL errors")
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _discussion_page(variables: dict[str, str | int], errors: list[str]) -> dict[str, Any]:
    """Extract discussion data without treating missing repository or discussion records as empty threads."""
    data = _request(_DISCUSSION_QUERY, variables, errors, "comments")
    repository = data.get("repository")
    discussion = repository.get("discussion") if isinstance(repository, dict) else None
    if not isinstance(discussion, dict):
        errors.append("comments: missing discussion")
        return {}
    return discussion


def _reply_page(comment_id: str, cursor: str, errors: list[str]) -> Any:
    """Fetch the next reply page bound to its exact parent and nested cursor."""
    data = _request(_REPLIES_QUERY, {"commentId": comment_id, "after": cursor}, errors, "replies")
    node = data.get("node")
    if not isinstance(node, dict) or node.get("id") != comment_id:
        errors.append("replies: missing or mismatched parent")
        return None
    return node.get("replies")


def _append_nodes(
    incoming: list[Any], nodes: list[dict], seen: set[str], limit: int, errors: list[str], label: str
) -> bool:
    """Append unique identified nodes within the remaining budget and report omitted unique nodes."""
    truncated = False
    for node in incoming:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]:
            errors.append(f"{label}: missing node id")
            continue
        if node["id"] in seen:
            continue
        if len(nodes) >= limit:
            truncated = True
            continue
        seen.add(node["id"])
        nodes.append(node)
    return truncated


def _paginate(
    initial: Any,
    next_page: Callable[[str], Any],
    label: str,
    limit: int,
    errors: list[str],
    seen: set[str] | None = None,
) -> dict[str, Any]:
    """Merge one connection, rejecting absent pagination evidence, cycles and exhausted budgets."""
    nodes: list[dict] = []
    seen = seen if seen is not None else set()
    cursors: set[str] = set()
    page = initial
    page_info: dict[str, Any] = {"hasNextPage": True, "endCursor": None}
    while True:
        if not isinstance(page, dict) or not isinstance(page.get("nodes"), list):
            errors.append(f"{label}: missing connection page")
            break
        before = len(nodes)
        truncated = _append_nodes(page["nodes"], nodes, seen, limit, errors, label)
        info = page.get("pageInfo")
        if not isinstance(info, dict) or not isinstance(info.get("hasNextPage"), bool):
            errors.append(f"{label}: missing page status")
            break
        page_info = dict(info)
        if truncated or (len(nodes) >= limit and info["hasNextPage"]):
            errors.append(f"{label}: {'reply' if label == 'replies' else 'comment'} cap reached")
            page_info["hasNextPage"] = True
            break
        if not info["hasNextPage"]:
            break
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            errors.append(f"{label}: missing or repeated cursor")
            break
        if len(nodes) == before:
            errors.append(f"{label}: next page made no progress")
            break
        cursors.add(cursor)
        page = next_page(cursor)
    return {"nodes": nodes, "pageInfo": page_info}


def fetch_discussion(repo: str, number: int) -> dict[str, Any]:
    """Collect bounded comments and replies in parent order, carrying all completeness evidence in the object."""
    owner, name = repo.split("/", 1)
    variables: dict[str, str | int] = {"owner": owner, "repo": name, "number": number}
    errors: list[str] = []
    discussion = _discussion_page(variables, errors)
    comments = _paginate(
        discussion.get("comments"),
        lambda cursor: _discussion_page({**variables, "after": cursor}, errors).get("comments"),
        "comments",
        _CAP,
        errors,
    )
    seen_replies: set[str] = set()
    for comment in comments["nodes"]:
        comment["replies"] = _paginate(
            comment.get("replies"),
            lambda cursor, parent=comment["id"]: _reply_page(parent, cursor, errors),
            "replies",
            _CAP - len(seen_replies),
            errors,
            seen_replies,
        )
    discussion["comments"] = comments
    discussion["_fetch"] = {
        "status": "incomplete" if errors else "complete",
        "comments": len(comments["nodes"]),
        "replies": len(seen_replies),
        "errors": errors,
    }
    return discussion


def main(argv: list[str] | None = None) -> int:
    """Write the discussion object and emit a bounded status summary for the caller's cache gate."""
    parser = argparse.ArgumentParser(description="Fetch and merge a GitHub discussion conversation.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--number", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", args.repo) or args.number <= 0:
        parser.error("repo must be OWNER/REPO and number must be positive")
    discussion = fetch_discussion(args.repo, args.number)
    status = discussion["_fetch"]
    try:
        write_private_text(args.output, json.dumps(discussion, ensure_ascii=False) + "\n")
    except OSError:
        print("discussion output could not be written", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {key: status[key] for key in ("status", "comments", "replies")} | {"error_count": len(status["errors"])}
        )
    )
    return int(status["status"] != "complete")


if __name__ == "__main__":
    raise SystemExit(main())
