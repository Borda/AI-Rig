#!/usr/bin/env python3
"""Generate benchmarks/tasks-oss.json from real closed pytorch-lightning issues.

Scrapes closed issues from Lightning-AI/pytorch-lightning via the ``gh`` CLI,
keeps only issues that were resolved by a *merged* pull request touching between
``--min-py-files`` and ``--max-py-files`` Python files, and emits a task file in
the same repo/tasks schema as ``tasks-bench.json``.

Usage:
    python generate-tasks-real-issues.py [--output tasks-oss.json] [--limit 20] \
        [--min-py-files 1] [--max-py-files 5]

    # Produce a small real sample
    python generate-tasks-real-issues.py --output tasks-oss.json --limit 10

Requirements:
    - ``gh`` CLI on PATH, authenticated (``gh auth status``)
    - network access to github.com

Scoring (downstream harness contract):
    Each task's ``ground_truth.files_changed`` is the *set* of source Python files
    the merged PR modified (test files excluded). A harness run produces a set of
    predicted files. With GT = ground_truth.files_changed and FOUND = prediction:

        recall    = len(FOUND & GT) / len(GT)         # coverage of true files
        precision = len(FOUND & GT) / len(FOUND)       # correctness of guesses

    ``recall`` is the primary metric (did the agent find the files that actually
    needed changing); ``precision`` penalises over-broad predictions. Both are in
    [0, 1] with higher being better. ``file_count`` equals ``len(GT)`` and drives
    the ``difficulty`` label (1 -> simple, 2-3 -> medium, 4-5 -> hard).

Provenance fields:
    - ``type`` / ``source`` = "real_issue"  -> task derives from a real GH issue
    - ``scoreable`` = True                  -> has non-empty ground truth set
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fire

REPO = "Lightning-AI/pytorch-lightning"
REPO_URL = f"https://github.com/{REPO}"
DEFAULT_OUTPUT = Path(__file__).parent / "tasks-oss.json"

PROMPT_BODY_LIMIT = 1200

# Titles that carry no actionable signal -> skip the issue.
GENERIC_TITLES = {"bug", "question", "help", "feature request", "feature", "issue", "error"}

# How many closed issues to fetch per qualifying task wanted. Issues that fail the
# merged-PR / file-count filters are common, so we over-fetch.
FETCH_MULTIPLIER = 12
FETCH_FLOOR = 60


class GenerationError(RuntimeError):
    """Raised when issue generation cannot proceed (e.g. ``gh`` auth failure)."""


@dataclass
class PullRequestInfo:
    """Resolved merged-PR metadata for a single issue.

    Attributes:
        number: PR number.
        source_files: Source ``.py`` files changed (test files excluded).
        py_file_count: Count of all ``.py`` files changed (tests included).
        closes_issue: True when the PR body contains an explicit closing keyword
            (``closes/fixes/resolves #N``) referencing the source issue.
        source_changes: Per-source-file change size (``additions + deletions``), keyed by
            repo-relative path. Populated from the ``gh pr view --json files`` payload; ``None``
            when the payload carried no change-size data (e.g. a hand-authored stub PR). Drives the
            most-changed primary-module heuristic in :func:`select_primary_module`.
    """

    number: int
    source_files: list[str]
    py_file_count: int
    closes_issue: bool = False
    source_changes: dict[str, int] | None = None


@dataclass
class IssueRecord:
    """A closed issue plus its resolving merged PR.

    Attributes:
        number: Issue number.
        title: Issue title (first prompt line).
        body: Issue body (truncated for the prompt).
        pr: Resolved merged-PR info.
    """

    number: int
    title: str
    body: str
    pr: PullRequestInfo


# ---- GH CLI WRAPPERS ----


def _run_gh(args: list[str], *, timeout: int = 60) -> str:
    """Run a ``gh`` command and return stdout.

    Args:
        args: Arguments following the ``gh`` executable.
        timeout: Hard timeout in seconds.

    Returns:
        Captured stdout (stripped).

    Raises:
        GenerationError: If ``gh`` is missing, times out, or exits non-zero.
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GenerationError("`gh` CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GenerationError(f"`gh {' '.join(args)}` timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise GenerationError(f"`gh {' '.join(args)}` failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _gh_json(args: list[str], *, timeout: int = 60) -> Any:
    """Run a ``gh`` command expected to emit JSON and parse it.

    Args:
        args: Arguments following the ``gh`` executable.
        timeout: Hard timeout in seconds.

    Returns:
        Parsed JSON (object or list); ``None`` on empty output.
    """
    out = _run_gh(args, timeout=timeout)
    if not out:
        return None
    return json.loads(out)


def check_gh_auth() -> bool:
    """Return True if ``gh`` is installed and authenticated, else False."""
    try:
        _run_gh(["auth", "status"], timeout=15)
        return True
    except GenerationError:
        return False


# ---- FETCH + FILTER ----


def fetch_closed_issues(max_issues: int) -> list[dict[str, Any]]:
    """Fetch closed issues (not PRs) ordered most-recently-updated first.

    Pages manually with a bounded loop rather than ``--paginate``: the latter
    walks every page of a large closed-issue set and times out. We stop as soon
    as ``max_issues`` non-PR issues are collected.

    Args:
        max_issues: Upper bound on issues to return after PR exclusion.

    Returns:
        List of raw issue dicts with ``number``, ``title``, ``body`` keys.
    """
    per_page = 100
    issues: list[dict[str, Any]] = []
    page = 1
    while len(issues) < max_issues:
        raw = _gh_json(
            [
                "api",
                f"repos/{REPO}/issues?state=closed&sort=updated&direction=desc&per_page={per_page}&page={page}",
            ],
            timeout=60,
        )
        items: list[dict[str, Any]] = raw if isinstance(raw, list) else []
        if not items:
            break  # no more pages
        for item in items:
            # The issues endpoint returns PRs too; they carry a "pull_request" key.
            if item.get("pull_request") is not None:
                continue
            issues.append(item)
            if len(issues) >= max_issues:
                break
        page += 1
    return issues


def is_meaningful_issue(title: str, body: str | None) -> bool:
    """Return True if the issue has a specific title and non-empty body.

    Args:
        title: Issue title.
        body: Issue body (may be None).

    Returns:
        True when the title is specific and the body is non-trivial.
    """
    cleaned_title = title.strip().lower()
    if not cleaned_title or cleaned_title in GENERIC_TITLES:
        return False
    return bool(body and body.strip())


def find_merged_pr_numbers(issue_number: int) -> list[int]:
    """Return PR numbers cross-referenced from an issue's timeline.

    Args:
        issue_number: Issue to inspect.

    Returns:
        Candidate PR numbers (merged status not yet verified).
    """
    timeline = _gh_json(
        ["api", f"repos/{REPO}/issues/{issue_number}/timeline?per_page=100"],
        timeout=60,
    )
    if not isinstance(timeline, list):
        return []
    pr_numbers: list[int] = []
    for event in timeline:
        if event.get("event") != "cross-referenced":
            continue
        source_issue = (event.get("source") or {}).get("issue") or {}
        if source_issue.get("pull_request") is None:
            continue
        number = source_issue.get("number")
        if isinstance(number, int) and number not in pr_numbers:
            pr_numbers.append(number)
    return pr_numbers


def _pr_closes_issue(body: str, issue_number: int) -> bool:
    """Return True if the PR body contains an explicit closing keyword for *issue_number*.

    Matches GitHub's auto-close syntax: ``closes/fixes/resolves #N`` (case-insensitive,
    with optional surrounding whitespace or punctuation).

    Args:
        body: PR body text.
        issue_number: Issue number to check for.

    Returns:
        True when a closing keyword + issue reference is found.
    """
    pattern = rf"(?:closes|fixes|resolves|close|fix|resolve)\s*#\s*{issue_number}\b"
    return bool(re.search(pattern, body, re.IGNORECASE))


def resolve_merged_pr(
    pr_numbers: list[int], min_py: int, max_py: int, issue_number: int | None = None
) -> PullRequestInfo | None:
    """Find the best merged PR matching the Python-file-count window.

    Prefers a PR whose body explicitly closes *issue_number* as a provenance check.
    Falls back to any cross-referenced merged PR that meets the file-count window,
    with ``closes_issue=False`` recorded for downstream review.

    Args:
        pr_numbers: Candidate PR numbers from the issue timeline.
        min_py: Minimum number of changed ``.py`` files (tests included).
        max_py: Maximum number of changed ``.py`` files (tests included).
        issue_number: Source issue number used for closing-keyword verification.
            Pass ``None`` to skip the check (backwards-compatible; all results
            will have ``closes_issue=False``).

    Returns:
        Matching :class:`PullRequestInfo`, or ``None`` if no candidate qualifies.
    """
    candidates: list[PullRequestInfo] = []
    for number in pr_numbers:
        info = _inspect_pr(number, min_py, max_py, issue_number=issue_number)
        if info is not None:
            candidates.append(info)
    if not candidates:
        return None
    # Prefer explicitly closing PRs; fall back to first cross-referenced candidate.
    for info in candidates:
        if info.closes_issue:
            return info
    fallback = candidates[0]
    if issue_number is not None:
        # No candidate carried a closes/fixes/resolves keyword — the issue<->PR linkage rests only on a
        # timeline cross-reference, which is weaker provenance. Surface it so the fallback is not silent
        # (the closes_issue=False flag already rides through to the task's pr_closes_issue field).
        print(
            f"! weak provenance: issue #{issue_number} -> PR #{fallback.number} "
            "(no closes/fixes/resolves keyword; linkage inferred from timeline cross-reference)",
            file=sys.stderr,
        )
    return fallback


def _inspect_pr(number: int, min_py: int, max_py: int, issue_number: int | None = None) -> PullRequestInfo | None:
    """Inspect a single PR for merged status and file-count eligibility.

    Args:
        number: PR number.
        min_py: Minimum changed ``.py`` files (tests included).
        max_py: Maximum changed ``.py`` files (tests included).
        issue_number: Source issue number for closing-keyword check (optional).

    Returns:
        :class:`PullRequestInfo` if merged and within the window, else ``None``.
    """
    try:
        data = _gh_json(
            ["pr", "view", str(number), "--repo", REPO, "--json", "state,mergedAt,files,body"],
            timeout=60,
        )
    except GenerationError:
        return None
    if not data or data.get("state") != "MERGED" or not data.get("mergedAt"):
        return None
    files = [f for f in data.get("files", []) if isinstance(f, dict) and f.get("path")]
    paths = [f["path"] for f in files]
    py_paths = [p for p in paths if p.endswith(".py")]
    if not (min_py <= len(py_paths) <= max_py):
        return None
    source_files = [p for p in py_paths if not _is_test_path(p)]
    if not source_files:
        return None
    source_set = set(source_files)
    source_changes = {
        f["path"]: int(f.get("additions") or 0) + int(f.get("deletions") or 0) for f in files if f["path"] in source_set
    }
    closes = False
    if issue_number is not None:
        closes = _pr_closes_issue(data.get("body") or "", issue_number)
    return PullRequestInfo(
        number=number,
        source_files=source_files,
        py_file_count=len(py_paths),
        closes_issue=closes,
        source_changes=source_changes,
    )


def _is_test_path(path: str) -> bool:
    """Return True if a path is a test file (excluded from ground truth)."""
    parts = path.split("/")
    if any(part in {"tests", "test"} for part in parts):
        return True
    basename = parts[-1]
    return basename.startswith("test_") or basename.endswith("_test.py") or basename == "conftest.py"


# ---- TASK ASSEMBLY ----


def difficulty_for(file_count: int) -> str:
    """Map a source-file count to a difficulty label.

    Args:
        file_count: Number of source files in the ground truth.

    Returns:
        ``"simple"`` (1), ``"medium"`` (2-3), or ``"hard"`` (4+).
    """
    if file_count <= 1:
        return "simple"
    if file_count <= 3:
        return "medium"
    return "hard"


def module_for(path: str) -> str:
    """Convert a source file path to a dotted module name.

    Strips a leading ``src/`` segment, drops the ``.py`` suffix, and replaces
    path separators with dots. ``__init__.py`` collapses to its package name.

    Args:
        path: Repo-relative source path (e.g. ``src/lightning/pytorch/x.py``).

    Returns:
        Dotted module name (e.g. ``lightning.pytorch.x``).
    """
    cleaned = path
    cleaned = cleaned.removeprefix("src/")
    if cleaned.endswith("/__init__.py"):
        cleaned = cleaned[: -len("/__init__.py")]
    elif cleaned.endswith(".py"):
        cleaned = cleaned[: -len(".py")]
    return cleaned.replace("/", ".")


def select_primary_module(source_files: list[str], source_changes: dict[str, int] | None) -> tuple[str, str]:
    """Pick the primary module for a multi-file issue and report how it was chosen.

    Prefers the module of the *most-changed* source file (largest ``additions + deletions``),
    breaking ties by original position in ``source_files`` so the choice is deterministic. When no
    change-size signal is available (``source_changes`` is ``None``, empty, or all-zero) the first
    file is used and the basis is reported as ``"first_file"`` so the arbitrariness is visible to a
    downstream reviewer rather than silently baked in.

    Args:
        source_files: Ordered repo-relative source paths (non-empty).
        source_changes: Per-path change size, or ``None`` when unavailable.

    Returns:
        A ``(dotted_module, basis)`` pair; ``basis`` is ``"most_changed"`` or ``"first_file"``.

    Examples:
        >>> select_primary_module(["src/a.py", "src/b.py"], {"src/a.py": 3, "src/b.py": 40})
        ('b', 'most_changed')
        >>> select_primary_module(["src/a.py", "src/b.py"], None)
        ('a', 'first_file')
    """
    changes = source_changes or {}
    if any(v > 0 for v in changes.values()):
        primary = max(source_files, key=lambda p: (changes.get(p, 0), -source_files.index(p)))
        return module_for(primary), "most_changed"
    return module_for(source_files[0]), "first_file"


def build_prompt(title: str, body: str) -> str:
    """Build the task prompt: title line plus a truncated body.

    Args:
        title: Issue title.
        body: Issue body.

    Returns:
        Title and truncated body joined by a blank line (two newlines).
    """
    truncated = body.strip()[:PROMPT_BODY_LIMIT]
    return f"{title.strip()}\n\n{truncated}"


def build_task(index: int, record: IssueRecord) -> dict[str, Any]:
    """Assemble a single task dict in the tasks-oss schema.

    Args:
        index: 1-based ordinal used to build the task id (``OSS-NN``).
        record: Qualifying issue plus resolved merged PR.

    Returns:
        Task dict matching the tasks-bench/tasks-oss schema.
    """
    source_files = record.pr.source_files
    file_count = len(source_files)
    primary_module, primary_module_basis = select_primary_module(source_files, record.pr.source_changes)
    return {
        "id": f"OSS-{index:02d}",
        "type": "real_issue",
        "source": "real_issue",
        "workflow_subtype": "pre_implementation_research",
        "difficulty": difficulty_for(file_count),
        "issue_number": record.number,
        "issue_url": f"{REPO_URL}/issues/{record.number}",
        "pr_number": record.pr.number,
        "pr_url": f"{REPO_URL}/pull/{record.pr.number}",
        "prompt": build_prompt(record.title, record.body),
        "ground_truth": {
            "files_changed": source_files,
            "file_count": file_count,
        },
        "primary_module": primary_module,
        "primary_module_basis": primary_module_basis,
        "scoreable": True,
        "pr_closes_issue": record.pr.closes_issue,
    }


def collect_records(limit: int, min_py: int, max_py: int) -> list[IssueRecord]:
    """Scrape and filter issues until ``limit`` qualifying records are found.

    Args:
        limit: Target number of tasks.
        min_py: Minimum changed ``.py`` files (tests included).
        max_py: Maximum changed ``.py`` files (tests included).

    Returns:
        Up to ``limit`` qualifying :class:`IssueRecord` instances.
    """
    budget = max(limit * FETCH_MULTIPLIER, FETCH_FLOOR)
    issues = fetch_closed_issues(budget)
    records: list[IssueRecord] = []
    for issue in issues:
        title = issue.get("title") or ""
        body = issue.get("body") or ""
        number = issue.get("number")
        if not isinstance(number, int) or not is_meaningful_issue(title, body):
            continue
        pr_numbers = find_merged_pr_numbers(number)
        if not pr_numbers:
            continue
        pr = resolve_merged_pr(pr_numbers, min_py, max_py, issue_number=number)
        if pr is None:
            continue
        records.append(IssueRecord(number=number, title=title, body=body, pr=pr))
        verified = "✓" if pr.closes_issue else "~"
        print(
            f"  [{len(records)}/{limit}] issue #{number} -> PR #{pr.number} ({pr.py_file_count} py) {verified}",
            file=sys.stderr,
        )
        if len(records) >= limit:
            break
    return records


def build_document(records: list[IssueRecord]) -> dict[str, Any]:
    """Wrap task records in the top-level tasks-oss document.

    Args:
        records: Qualifying issue records.

    Returns:
        Document with ``repo`` and ``tasks`` keys.
    """
    tasks = [build_task(i + 1, rec) for i, rec in enumerate(records)]
    return {
        "repo": {
            "name": "pytorch-lightning",
            "namespace": ["lightning"],
            "url": REPO_URL,
        },
        "tasks": tasks,
    }


# ---- STUB FALLBACK ----


def stub_document() -> dict[str, Any]:
    """Return a hand-authored placeholder document for offline harness work.

    Returns:
        A tasks-oss document with two placeholder tasks in the real schema.
    """
    placeholders = [
        IssueRecord(
            number=21708,
            title="CombinedLoader hangs when one dataloader is exhausted",
            body="When using CombinedLoader in max_size_cycle mode, the loop fails to "
            "terminate if a child dataloader raises StopIteration early. (placeholder)",
            pr=PullRequestInfo(
                number=21709,
                source_files=["src/lightning/pytorch/utilities/combined_loader.py"],
                py_file_count=2,
            ),
        ),
        IssueRecord(
            number=21500,
            title="Timer callback does not respect interval on resume",
            body="Resuming training from a checkpoint resets the Timer callback's "
            "elapsed time instead of restoring it. (placeholder)",
            pr=PullRequestInfo(
                number=21501,
                source_files=[
                    "src/lightning/pytorch/callbacks/timer.py",
                    "src/lightning/pytorch/trainer/connectors/checkpoint_connector.py",
                ],
                py_file_count=3,
            ),
        ),
    ]
    return build_document(placeholders)


# ---- CLI ----


def write_document(document: dict[str, Any], output: Path) -> None:
    """Write the document to disk as pretty-printed JSON.

    Args:
        document: tasks-oss document.
        output: Destination path.
    """
    output.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(
    output: Path = DEFAULT_OUTPUT,
    limit: int = 20,
    min_py_files: int = 1,
    max_py_files: int = 5,
) -> None:
    """Entry point: scrape issues and write tasks-oss.json (or a stub on failure).

    Args:
        output: Output JSON path.
        limit: Target number of tasks.
        min_py_files: Minimum changed .py files (tests included).
        max_py_files: Maximum changed .py files (tests included).
    """
    # fire passes CLI string args regardless of type annotation — coerce Path args explicitly.
    output = Path(output)

    if min_py_files < 1 or max_py_files < min_py_files:
        print("error: require 1 <= --min-py-files <= --max-py-files", file=sys.stderr)
        sys.exit(2)

    if not check_gh_auth():
        print("! gh auth unavailable - writing stub tasks-oss.json", file=sys.stderr)
        write_document(stub_document(), output)
        sys.exit(1)

    try:
        print(f"Scraping closed issues from {REPO} (target {limit} tasks)...", file=sys.stderr)
        records = collect_records(limit, min_py_files, max_py_files)
    except GenerationError as exc:
        print(f"! generation failed ({exc}) - writing stub tasks-oss.json", file=sys.stderr)
        write_document(stub_document(), output)
        sys.exit(1)

    if not records:
        print("! no qualifying issues found - writing stub tasks-oss.json", file=sys.stderr)
        write_document(stub_document(), output)
        sys.exit(1)

    document = build_document(records)
    write_document(document, output)
    print(f"Wrote {len(records)} tasks to {output}", file=sys.stderr)


if __name__ == "__main__":
    fire.Fire(main)
