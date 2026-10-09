#!/usr/bin/env python
"""fetch_gh_data_group2.py — Group 2 sequential gh API data fetch for oss:gh-scraper.

Runs **after** Group 1 has resolved the repo's default branch. Fetches the
content the documentation, CI and governance rubrics read: README,
CONTRIBUTING and SECURITY (root, ``.github/`` or ``docs/``), the changelog's
dated headings, the ``.github/`` and ``docs/`` listings, CODEOWNERS (``.github/``,
root or ``docs/``), the default branch's protection flag and rules, its newest
completed workflow runs, every workflow file's content, and
``.github/dependabot.yml`` (or ``.yaml``) — base64-decoding text content where the GitHub
Contents API returns it inline.

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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from shutil import which

#: Enforce safe owner/repo/branch shapes to defuse URL-path injection (A03:2021).
_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
#: Branch names allow ``/`` (e.g. ``release/1.x``) but no path traversal or shell metachars.
_BRANCH_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")

#: Max workflow files whose content is fetched into the ``workflow_files`` record. Every ``.yml``/``.yaml`` file in
#: ``.github/workflows/`` is read up to this bound; beyond it (or when a fetch fails) the record is ``partial``.
_WORKFLOW_FETCH_CAP = 50
#: Parallel workflow-content fetches — wide bursts of ``gh api`` calls trip GitHub's secondary rate limit.
_WORKFLOW_WORKERS = 6
#: Changelog lines kept verbatim from the top, for files that date entries without headings.
_CHANGELOG_HEAD_LINES = 10
#: Max changelog heading lines kept; beyond it the record is flagged ``truncated``.
_CHANGELOG_HEADING_CAP = 300
#: File-name stems of a changelog, in preference order.
_CHANGELOG_STEMS = ("changelog", "changes", "history", "news")
#: Markdown ATX heading (``## [1.2.0] - 2024-01-01``).
_ATX_HEADING_RE = re.compile(r"^#{1,6}\s")
#: Setext/reStructuredText underline marking the previous line as a heading.
_UNDERLINE_RE = re.compile(r"^(=+|-+|~+|\^+)\s*$")
#: Completed default-branch workflow runs fetched for the Axis 5 pass rate: one page, newest first, so the newest 20
#: counted runs survive the skipped/neutral/cancelled and non-CI-event exclusions.
_CI_RUNS_PER_PAGE = 100
#: Seconds allowed for the CI-runs page — the largest Group 2 response; the per-call default suits single small files.
_CI_RUNS_TIMEOUT = 30


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
        result = subprocess.run(  # noqa: S603
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


def _list_names(ctx: FetchContext, sub_path: str) -> list[str] | None:
    """List file names in a repository directory (``""`` = root).

    Returns:
        Names in API order, or ``None`` when the listing failed or was not a JSON array.
    """
    api_path = f"repos/{ctx.owner_repo}/contents" + (f"/{sub_path}" if sub_path else "")
    rc, stdout = _gh_call(ctx.gh, api_path, "[.[] | .name]", ctx.timeout)
    if rc != 0 or not stdout:
        return None
    try:
        names = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return [str(name) for name in names] if isinstance(names, list) else None


def _fetch_file_text(ctx: FetchContext, path: str) -> str | None:
    """Fetch and decode one repository file; ``None`` when absent or undecodable."""
    rc, raw = _gh_call(ctx.gh, f"repos/{ctx.owner_repo}/contents/{path}", ".content", ctx.timeout)
    if rc != 0 or not raw:
        return None
    text = _decode_b64(raw)
    if not text:
        print(f"[fetch_gh_data_group2] WARN: {path} base64 decode failed", file=sys.stderr)
        return None
    return text


def _stem_matches(names: list[str], stem: str) -> list[str]:
    """Names whose lower-cased stem (text before the first dot) equals ``stem``.

    Examples:
        >>> _stem_matches(["CONTRIBUTING.md", "contributing", "Contrib.md"], "contributing")
        ['CONTRIBUTING.md', 'contributing']
    """
    return [name for name in names if name.lower().split(".", 1)[0] == stem]


def _community_paths(
    stem: str, root: list[str] | None, github: list[str] | None, docs: list[str] | None = None
) -> list[str]:
    """Candidate paths for a community-health file, in GitHub's lookup order: root, ``.github/``, ``docs/``.

    A known listing contributes only the names it actually holds, in any extension (``CONTRIBUTING.rst``); an unknown
    listing contributes the conventional ``<STEM>.md`` guess, so a failed listing never hides an existing file.
    ``docs/`` is tried only when the root lists it (under its real name, ``Docs/`` included) or the root listing is
    unknown.

    Examples:
        >>> _community_paths("security", ["README.md", "docs"], [".keep", "SECURITY.md"])
        ['.github/SECURITY.md', 'docs/SECURITY.md']
        >>> _community_paths("contributing", ["docs"], [], ["index.md", "CONTRIBUTING.rst"])
        ['docs/CONTRIBUTING.rst']
        >>> _community_paths("contributing", None, None)
        ['CONTRIBUTING.md', '.github/CONTRIBUTING.md', 'docs/CONTRIBUTING.md']
        >>> _community_paths("security", ["Docs"], [])
        ['Docs/SECURITY.md']
    """
    default = f"{stem.upper()}.md"
    paths = _stem_matches(root, stem) if root is not None else [default]
    paths += (
        [f".github/{name}" for name in _stem_matches(github, stem)] if github is not None else [f".github/{default}"]
    )
    docs_dir = _docs_dir_name(root)
    if docs_dir is None:
        return paths
    if docs is not None:
        return paths + [f"{docs_dir}/{name}" for name in _stem_matches(docs, stem)]
    return [*paths, f"{docs_dir}/{default}"]


def _docs_dir_name(root: list[str] | None) -> str | None:
    """Name the ``docs/`` directory as the root lists it (GitHub paths are case-sensitive); ``None`` when absent.

    An unknown root listing yields the conventional ``docs``, so a failed listing never hides the directory.

    Examples:
        >>> _docs_dir_name(["Docs", "src"]), _docs_dir_name(["src"]), _docs_dir_name(None)
        ('Docs', None, 'docs')
    """
    if root is None:
        return "docs"
    return next((name for name in root if name.lower() == "docs"), None)


def changelog_outline(text: str) -> tuple[dict[str, list[str]], bool]:
    """Reduce a changelog to its first lines and heading lines, where entry dates live.

    Changelogs routinely run to hundreds of kilobytes with a long undated ``Unreleased`` section first, so the
    newest dated entry can sit far below the top. Headings (Markdown ATX, Setext or reStructuredText underlines)
    carry the release dates in every common format; the first lines cover files that date entries without them.

    Args:
        text: Decoded changelog.

    Returns:
        ``({"head": [...], "headings": [...]}, truncated)``; ``truncated`` when the heading cap was hit.

    Examples:
        >>> outline, cut = changelog_outline("# Changelog\\n\\n## [Unreleased]\\n- x\\n\\n1.0 (2024-01-02)\\n----\\n")
        >>> outline["headings"], cut
        (['# Changelog', '## [Unreleased]', '1.0 (2024-01-02)'], False)
    """
    lines = text.splitlines()
    headings: list[str] = []
    for index, line in enumerate(lines):
        if _ATX_HEADING_RE.match(line):
            headings.append(line.strip())
        elif index and _UNDERLINE_RE.match(line) and lines[index - 1].strip():
            headings.append(lines[index - 1].strip())
    outline = {"head": lines[:_CHANGELOG_HEAD_LINES], "headings": headings[:_CHANGELOG_HEADING_CAP]}
    return outline, len(headings) > _CHANGELOG_HEADING_CAP


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


def _fetch_github_dir(ctx: FetchContext) -> list[str] | None:
    """Fetch the ``.github/`` listing, append the ``github_dir`` record and return the names."""
    names = _list_names(ctx, ".github")
    if names is not None:
        _append_record(ctx.data_file, {"type": "github_dir", "data": names})
    return names


def _fetch_docs_dir(ctx: FetchContext, root: list[str] | None) -> list[str] | None:
    """Fetch the ``docs/`` listing when ``docs/`` may exist, append the ``docs_dir`` record and return the names.

    Without it a CONTRIBUTING or SECURITY file kept only in ``docs/`` was proven neither present nor absent: a failed
    ``docs/<STEM>.md`` fetch read as "file absent", and ``docs/CONTRIBUTING.rst`` was never tried. The listing uses the
    root entry's own name: a lower-case ``docs`` path 404s for a ``Docs/`` directory.
    """
    docs_dir = _docs_dir_name(root)
    if docs_dir is None:
        return None
    names = _list_names(ctx, docs_dir)
    if names is not None:
        _append_record(ctx.data_file, {"type": "docs_dir", "data": names})
    return names


def _fetch_community(ctx: FetchContext, record_type: str, stem: str, listings: tuple[list[str] | None, ...]) -> None:
    """Fetch the first existing community-health file (CONTRIBUTING, SECURITY) and append it with its ``source``."""
    root, github, docs = listings
    for path in _community_paths(stem, root, github, docs):
        text = _fetch_file_text(ctx, path)
        if text is not None:
            _append_record(ctx.data_file, {"type": record_type, "source": path, "data": text})
            return


def _fetch_changelog(ctx: FetchContext, root: list[str] | None) -> None:
    """Fetch the root changelog and append its outline as ``changelog_headings``."""
    if root is None:
        candidates = ["CHANGELOG.md"]
    else:
        candidates = [name for stem in _CHANGELOG_STEMS for name in _stem_matches(root, stem)]
    for path in candidates:
        text = _fetch_file_text(ctx, path)
        if text is None:
            continue
        outline, truncated = changelog_outline(text)
        record = {
            "type": "changelog_headings",
            "source": path,
            "bytes": len(text.encode("utf-8")),
            "truncated": truncated,
            "data": outline,
        }
        _append_record(ctx.data_file, record)
        return


def _fetch_codeowners(ctx: FetchContext, root: list[str] | None) -> None:
    """Fetch CODEOWNERS and append ``codeowners_text`` on success.

    Locations are tried in GitHub's lookup order — ``.github/``, root, then ``docs/`` (only when ``docs/`` may exist).
    """
    docs_dir = _docs_dir_name(root)
    for path in (".github/CODEOWNERS", "CODEOWNERS", *([f"{docs_dir}/CODEOWNERS"] if docs_dir else [])):
        text = _fetch_file_text(ctx, path)
        if text is not None:
            _append_record(ctx.data_file, {"type": "codeowners_text", "data": text, "source": path})
            return


def _fetch_branch_status(ctx: FetchContext, default_branch: str) -> None:
    """Fetch the default branch's ``protected`` flag (no admin rights needed); append ``default_branch_status``."""
    rc, stdout = _gh_call(
        ctx.gh, f"repos/{ctx.owner_repo}/branches/{default_branch}", "{protected: .protected}", ctx.timeout
    )
    if rc != 0 or not stdout:
        return
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return
    if isinstance(payload, dict) and isinstance(payload.get("protected"), bool):
        _append_record(ctx.data_file, {"type": "default_branch_status", "branch": default_branch, "data": payload})


def _fetch_ci_runs(ctx: FetchContext, default_branch: str) -> None:
    """Fetch the default branch's newest completed workflow runs; append ``ci_runs`` with the ``branch`` it covers.

    Scoped server-side to the default branch, so contributors' pull-request branches never fill the page; ``event`` and
    ``head_branch`` are kept because ``branch=`` matches a run's head branch, and a pull request from a fork's own
    ``main`` still comes back (the extractor samples only ``push``, ``schedule``, ``workflow_dispatch`` and
    ``merge_group`` runs). Completed runs only: pending runs never enter the pass-rate sample.
    """
    api_path = (
        f"repos/{ctx.owner_repo}/actions/runs?branch={default_branch}&status=completed&per_page={_CI_RUNS_PER_PAGE}"
    )
    jq = "[.workflow_runs[] | {conclusion, name, event, head_branch}]"
    rc, stdout = _gh_call(ctx.gh, api_path, jq, max(ctx.timeout, _CI_RUNS_TIMEOUT))
    if rc != 0 or not stdout:
        return
    try:
        runs = json.loads(stdout)
    except json.JSONDecodeError:
        return
    if isinstance(runs, list):
        record = {"type": "ci_runs", "branch": default_branch, "records": len(runs), "data": runs}
        _append_record(ctx.data_file, record)


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


def _fetch_workflow_contents(ctx: FetchContext, names: list[str]) -> tuple[list[str], list[str]]:
    """Fetch and decode the capped workflow files in parallel.

    Args:
        ctx: Shared fetch context (gh path, owner/repo, data file, timeout).
        names: Workflow file names (``.yml``/``.yaml``) from the directory listing.

    Returns:
        ``(pieces, failed)`` — ``"--- workflow: <name> ---\\n<text>"`` per decoded file in listing order, and the
        names whose fetch or decode failed.
    """
    capped = names[:_WORKFLOW_FETCH_CAP]
    with ThreadPoolExecutor(max_workers=min(_WORKFLOW_WORKERS, len(capped))) as pool:
        texts = list(pool.map(lambda name: _fetch_file_text(ctx, f".github/workflows/{name}"), capped))
    pieces = [f"--- workflow: {name} ---\n{text}" for name, text in zip(capped, texts) if text is not None]
    failed = [name for name, text in zip(capped, texts) if text is None]
    return pieces, failed


def _fetch_workflows(ctx: FetchContext) -> None:
    """List ``.github/workflows/`` and append the listing plus the content of every workflow file."""
    names = _list_names(ctx, ".github/workflows")
    if not names:
        return
    _append_record(ctx.data_file, {"type": "workflows_list", "data": names})
    files = [name for name in names if name.lower().endswith((".yml", ".yaml"))]
    if not files:
        return
    pieces, failed = _fetch_workflow_contents(ctx, files)
    if pieces:
        record = {
            "type": "workflow_files",
            "listed": len(files),
            "fetched": len(pieces),
            "failed": failed,
            "partial": len(pieces) < len(files),
            "data": "\n".join(pieces),
        }
        _append_record(ctx.data_file, record)


def _fetch_dependabot(ctx: FetchContext) -> None:
    """Fetch the Dependabot config metadata and append a ``dependabot_config`` record with its ``source``.

    GitHub reads ``.github/dependabot.yml`` or ``.github/dependabot.yaml``; the first one found wins.
    """
    for path in (".github/dependabot.yml", ".github/dependabot.yaml"):
        rc, stdout = _gh_call(ctx.gh, f"repos/{ctx.owner_repo}/contents/{path}", None, ctx.timeout)
        if rc != 0 or not stdout:
            continue
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            continue
        _append_record(ctx.data_file, {"type": "dependabot_config", "source": path, "data": payload})
        return


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


def _fetch_all(ctx: FetchContext, default_branch: str) -> None:
    """Run every Group 2 fetch; the root, ``.github/`` and ``docs/`` listings decide which community files exist."""
    root = _list_names(ctx, "")
    github = _fetch_github_dir(ctx)
    docs = _fetch_docs_dir(ctx, root)
    _fetch_readme(ctx)
    _fetch_community(ctx, "contributing_text", "contributing", (root, github, docs))
    _fetch_community(ctx, "security_text", "security", (root, github, docs))
    _fetch_changelog(ctx, root)
    _fetch_codeowners(ctx, root)
    _fetch_branch_status(ctx, default_branch)
    _fetch_branch_protection(ctx, default_branch)
    _fetch_ci_runs(ctx, default_branch)
    _fetch_workflows(ctx)
    _fetch_dependabot(ctx)


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
    _fetch_all(ctx, args.default_branch)

    print(f"[fetch_gh_data_group2] appended records → {data_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
