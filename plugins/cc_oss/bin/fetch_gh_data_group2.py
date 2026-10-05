#!/usr/bin/env python
"""fetch_gh_data_group2.py — Group 2 sequential gh API data fetch for oss:gh-scraper.

Runs **after** Group 1 has resolved the repo's root file list and default branch.
Fetches README, CONTRIBUTING.md, ``.github/`` listing, CODEOWNERS,
default-branch protection, workflow listing + first two workflow file
contents, and ``.github/dependabot.yml`` — base64-decoding text content
where the GitHub Contents API returns it inline.

Each fetched dataset is appended to ``--data-file`` as a single
JSON object on its own line (JSONL). 404s and other non-zero exits
are swallowed silently — the dataset is simply not appended,
matching Group 1's "tried, failed → absent record" contract.

Usage:
    fetch_gh_data_group2.py --owner <owner> --repo <repo>
                            --default-branch <branch>
                            --data-file <path>
                            [--cutoff <YYYY-MM-DD>]
                            [--timeout <secs>]

Exit: 0 on success (individual fetch failures non-fatal); 1 on bad args.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from shutil import which

# Enforce safe owner/repo/branch shapes to defuse URL-path injection (A03:2021).
_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
# Branch names allow ``/`` (e.g. ``release/1.x``) but no path traversal or shell metachars.
_BRANCH_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")

# Max workflow files whose content is fetched and concatenated into
# the ``workflow_files`` record. Mirrors the original shell pipeline's
# ``head -2`` and keeps the response payload bounded.
_WORKFLOW_FETCH_CAP = 2


@dataclass(frozen=True)
class FetchContext:
    """Bundles the per-run values every Group 2 fetcher needs.

    Attributes:
        gh: Absolute path to the ``gh`` binary.
        owner_repo: ``"<owner>/<repo>"`` slug for API paths.
        data_file: Output JSONL path; records appended one per line.
        timeout: Per-``gh``-call subprocess timeout in seconds.
    """

    gh: str
    owner_repo: str
    data_file: Path
    timeout: int


def _resolve(cmd: str) -> str:
    """Resolve a CLI tool to its absolute path.

    Args:
        cmd: Bare executable name (e.g. ``"gh"``).

    Returns:
        Absolute path to the executable.

    Raises:
        FileNotFoundError: If ``cmd`` is not present on ``PATH``.

    Examples:
        >>> import shutil
        >>> _resolve("gh") == shutil.which("gh") or shutil.which("gh") is None
        True
    """
    p = which(cmd)
    if p is None:
        raise FileNotFoundError(f"executable not found on PATH: {cmd}")
    return p


def _decode_b64(raw: str) -> str:
    """Decode base64 string from the GitHub Contents API.

    The API returns content wrapped to 60 columns with embedded newlines —
    ``base64.b64decode`` tolerates the whitespace. Returns the empty
    string for any decode failure (non-base64 input, mid-stream
    corruption, rate-limit interception).

    Args:
        raw: base64-encoded payload as returned by ``--jq '.content'``.

    Returns:
        UTF-8 decoded text; empty string on any failure or empty input.

    Examples:
        >>> _decode_b64("aGVsbG8=")
        'hello'
        >>> _decode_b64("")
        ''
        >>> _decode_b64("!!!not-base64!!!")
        ''
    """
    if not raw:
        return ""
    try:
        return base64.b64decode(raw, validate=False).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return ""


def _gh_call(gh: str, api_path: str, jq: str | None, timeout: int) -> tuple[int, str]:
    """Run a single ``gh api`` call.

    Args:
        gh: Absolute path to the gh binary.
        api_path: Path passed to ``gh api`` (e.g. ``repos/o/r/readme``).
        jq: Optional ``--jq`` expression; ``None`` for raw JSON output.
        timeout: Per-call timeout (seconds).

    Returns:
        ``(returncode, stdout)``; ``stdout`` stripped of trailing newline.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    cmd = [gh, "api", api_path]
    if jq is not None:
        cmd += ["--jq", jq]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    return result.returncode, result.stdout.strip()


def _append_record(data_file: Path, record: dict[str, object]) -> None:
    """Append a single JSON record to ``data_file`` as a JSONL line.

    Args:
        data_file: Destination JSONL path; parent dir must already exist.
        record: JSON-serializable mapping written as one line.

    Examples:
        No doctest — file I/O; covered by pytest.
    """
    with data_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False))
        fh.write("\n")


def _fetch_readme(ctx: FetchContext) -> None:
    """Fetch repo README and append ``readme_content`` record on success."""
    rc, raw = _gh_call(ctx.gh, f"repos/{ctx.owner_repo}/readme", ".content", ctx.timeout)
    if rc != 0 or not raw:
        return
    text = _decode_b64(raw)
    if not text:
        print("[fetch_gh_data_group2] WARN: README base64 decode failed", file=sys.stderr)
        return
    _append_record(ctx.data_file, {"type": "readme_content", "data": text})


def _fetch_contributing(ctx: FetchContext) -> None:
    """Fetch root CONTRIBUTING.md and append ``contributing_text`` record on success."""
    rc, raw = _gh_call(ctx.gh, f"repos/{ctx.owner_repo}/contents/CONTRIBUTING.md", ".content", ctx.timeout)
    if rc != 0 or not raw:
        return
    text = _decode_b64(raw)
    if not text:
        print("[fetch_gh_data_group2] WARN: CONTRIBUTING.md base64 decode failed", file=sys.stderr)
        return
    _append_record(ctx.data_file, {"type": "contributing_text", "data": text})


def _fetch_github_dir(ctx: FetchContext) -> None:
    """Fetch ``.github/`` listing and append ``github_dir`` record on success."""
    rc, stdout = _gh_call(ctx.gh, f"repos/{ctx.owner_repo}/contents/.github", "[.[] | .name]", ctx.timeout)
    if rc != 0 or not stdout:
        return
    try:
        names = json.loads(stdout)
    except json.JSONDecodeError:
        return
    _append_record(ctx.data_file, {"type": "github_dir", "data": names})


def _fetch_codeowners(ctx: FetchContext) -> None:
    """Fetch CODEOWNERS (try ``.github/`` then root) and append ``codeowners_text`` on success."""
    for path in (".github/CODEOWNERS", "CODEOWNERS"):
        rc, raw = _gh_call(ctx.gh, f"repos/{ctx.owner_repo}/contents/{path}", ".content", ctx.timeout)
        if rc != 0 or not raw:
            continue
        text = _decode_b64(raw)
        if not text:
            print(
                f"[fetch_gh_data_group2] WARN: {path} base64 decode failed",
                file=sys.stderr,
            )
            continue
        _append_record(ctx.data_file, {"type": "codeowners_text", "data": text, "source": path})
        return


def _fetch_branch_protection(ctx: FetchContext, default_branch: str) -> None:
    """Fetch default-branch protection settings; append ``branch_protection`` record on success."""
    rc, stdout = _gh_call(ctx.gh, f"repos/{ctx.owner_repo}/branches/{default_branch}/protection", None, ctx.timeout)
    if rc != 0 or not stdout:
        return
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return
    _append_record(
        ctx.data_file,
        {"type": "branch_protection", "branch": default_branch, "data": payload},
    )


def _fetch_workflow_contents(ctx: FetchContext, names: list[object]) -> list[str]:
    """Fetch and decode the content of each capped workflow file name.

    Args:
        ctx: Shared fetch context (gh path, owner/repo, data file, timeout).
        names: Workflow file names as returned by the directory listing.

    Returns:
        ``"--- workflow: <name> ---\\n<text>"`` pieces for every name whose
        content was fetched and decoded successfully.
    """
    pieces: list[str] = []
    for name in names[:_WORKFLOW_FETCH_CAP]:
        if not isinstance(name, str) or not name:
            continue
        rc, raw = _gh_call(
            ctx.gh,
            f"repos/{ctx.owner_repo}/contents/.github/workflows/{name}",
            ".content",
            ctx.timeout,
        )
        if rc != 0 or not raw:
            continue
        text = _decode_b64(raw)
        if not text:
            print(
                f"[fetch_gh_data_group2] WARN: workflow {name} base64 decode failed",
                file=sys.stderr,
            )
            continue
        pieces.append(f"--- workflow: {name} ---\n{text}")
    return pieces


def _fetch_workflows(ctx: FetchContext) -> None:
    """List ``.github/workflows/`` and append both directory + concatenated content records."""
    rc, stdout = _gh_call(ctx.gh, f"repos/{ctx.owner_repo}/contents/.github/workflows", "[.[] | .name]", ctx.timeout)
    if rc != 0 or not stdout:
        return
    try:
        names = json.loads(stdout)
    except json.JSONDecodeError:
        return
    if not isinstance(names, list) or not names:
        return
    _append_record(ctx.data_file, {"type": "workflows_list", "data": names})

    pieces = _fetch_workflow_contents(ctx, names)
    if pieces:
        _append_record(
            ctx.data_file,
            {"type": "workflow_files", "data": "\n".join(pieces)},
        )


def _fetch_dependabot(ctx: FetchContext) -> None:
    """Fetch ``.github/dependabot.yml`` metadata; append ``dependabot_config`` record on success."""
    rc, stdout = _gh_call(ctx.gh, f"repos/{ctx.owner_repo}/contents/.github/dependabot.yml", None, ctx.timeout)
    if rc != 0 or not stdout:
        return
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return
    _append_record(ctx.data_file, {"type": "dependabot_config", "data": payload})


def _validate_args(owner: str, repo: str, default_branch: str, data_file: str) -> str | None:
    """Validate CLI args; return error message on failure, ``None`` on success.

    Args:
        owner: GitHub owner or organization name.
        repo: GitHub repository name.
        default_branch: Repository default branch name.
        data_file: Output JSONL path.

    Returns:
        ``None`` when all args valid; a single-line error message otherwise.

    Examples:
        >>> _validate_args("owner", "repo", "main", "/opt/out.jsonl") is None
        True
        >>> _validate_args("", "repo", "main", "/opt/out.jsonl")
        '--owner required'
        >>> _validate_args("owner", "repo", "..", "/opt/out.jsonl")
        "--default-branch must match '[A-Za-z0-9._/-]+', got: '..'"
    """
    if not owner:
        return "--owner required"
    if not _NAME_RE.match(owner):
        return f"--owner must match '[A-Za-z0-9._-]+', got: {owner!r}"
    if not repo:
        return "--repo required"
    if not _NAME_RE.match(repo):
        return f"--repo must match '[A-Za-z0-9._-]+', got: {repo!r}"
    if not default_branch:
        return "--default-branch required"
    if not _BRANCH_RE.match(default_branch) or ".." in default_branch:
        return f"--default-branch must match '[A-Za-z0-9._/-]+', got: {default_branch!r}"
    if not data_file:
        return "--data-file required"
    return None


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — append Group 2 records to the JSONL data file.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 1 on bad args; 0 on success (individual failures non-fatal).

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    parser = argparse.ArgumentParser(
        prog="fetch_gh_data_group2",
        description="Group 2 sequential gh API data fetch for oss:gh-scraper.",
    )
    parser.add_argument("--owner", required=False, default="", help="GitHub owner or org.")
    parser.add_argument("--repo", required=False, default="", help="GitHub repository name.")
    parser.add_argument(
        "--default-branch",
        required=False,
        default="",
        help="Repository default branch (from Group 1 repo_metadata).",
    )
    parser.add_argument(
        "--data-file",
        required=False,
        default="",
        help="Output JSONL path; records appended one per line.",
    )
    parser.add_argument(
        "--cutoff",
        required=False,
        default="",
        help="Optional ISO date cutoff (reserved; unused at present).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Per-call gh subprocess timeout in seconds (default: 10).",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on bad args — normalize to 1 to match Group 1 contract.
        return 1 if exc.code not in (0, None) else 0

    err = _validate_args(args.owner, args.repo, args.default_branch, args.data_file)
    if err is not None:
        print(f"fetch_gh_data_group2: {err}", file=sys.stderr)
        return 1

    data_file = Path(args.data_file)
    data_file.parent.mkdir(parents=True, exist_ok=True)

    owner_repo = f"{args.owner}/{args.repo}"
    gh = _resolve("gh")
    ctx = FetchContext(gh=gh, owner_repo=owner_repo, data_file=data_file, timeout=args.timeout)

    _fetch_readme(ctx)
    _fetch_contributing(ctx)
    _fetch_github_dir(ctx)
    _fetch_codeowners(ctx)
    _fetch_branch_protection(ctx, args.default_branch)
    _fetch_workflows(ctx)
    _fetch_dependabot(ctx)

    print(f"[fetch_gh_data_group2] appended records → {data_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
