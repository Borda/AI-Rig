#!/usr/bin/env python
"""pr_drift.py — measure how far a pull request has drifted from its base branch.

Used by ``/oss:analyse <PR>`` to give the PR-vitality verdict hard evidence: age of the last commit, how far the base
moved since the PR branched, and — the signal that matters most for long-stale PRs — which of the files the PR touches
were changed, renamed or removed on the base since the merge base.

Drift is read from the commit graph: one ``compare/{merge_base}...{base}`` call lists what the base changed, so the
merge-base commit itself is never counted and a file the base moved is reported as ``renamed``, not removed. When that
list is unavailable or may be cut off (it holds at most 300 files), per-file commit probes by date take over; that
fallback is named in ``incomplete`` and in ``source``.

Output is one JSON object on stdout::

    {"number", "url", "base", "head_sha", "state", "mergeable", "age_days", "last_commit_days", "merge_base_age_days",
     "ahead_by", "behind_by", "files_total", "files_considered", "source": "compare|per-file|none",
     "drifted_files": [{"path", "kind": "changed|renamed|removed|absent", "renamed_to", "weight"}],
     "drifted_share", "removed_count", "recent_base_commits": [{"path", "sha", "date", "subject"}],
     "linked_issues": [url, ...], "staleness": "fresh|aging|stale|unknown|n/a",
     "drift": "none|light|heavy|unknown|n/a", "incomplete": [...]}

``weight`` is the lines the base changed in that file (``source=compare``) or its commit count (``source=per-file``).
``mergeable`` is GitHub's own value and is often ``UNKNOWN`` on old PRs; it is not evidence. A failed GitHub call never
aborts the run: it adds a reason to ``incomplete`` and the field stays ``null`` or ``unknown``, so the caller can
state what the verdict could not see. A PR that is not open gets ``n/a`` grades and no drift measurement.

Usage:
    pr_drift.py --number <N> [--repo owner/repo] [--max-files 40] [--timeout SECS]

Exit codes:
    0 — JSON written (possibly with ``incomplete`` entries)
    1 — bad arguments, ``gh`` missing, or the PR itself could not be fetched
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from shutil import which
from urllib.parse import quote

#: Age in days of the last commit at which a PR counts as aging / stale.
AGING_DAYS, STALE_DAYS = 30, 90
#: Drift is heavy when this share of files changed on base and at least ``HEAVY_MIN_FILES`` did: one base commit on a
#: one- or two-file PR is not heavy drift.
HEAVY_SHARE, HEAVY_MIN_FILES = 0.25, 3
#: ``compare`` lists at most this many files, so a list this long may be cut off.
COMPARE_FILE_CAP = 300
#: Files whose recent base commits are returned for the verdict to read.
RECENT_FILES = 5


def grade(
    last_commit_days: int | None, drifted_share: float | None, drifted_count: int, removed_count: int
) -> tuple[str, str]:
    """Return ``(staleness, drift)`` from the raw measurements.

    Staleness depends on the age of the last commit alone. Drift is separate: ``heavy`` when any touched file no longer
    exists on base, or when at least three files and a quarter of them changed; ``light`` when some changed; ``none``
    when none did. A measurement that is unknown (``None``) yields ``unknown``, never a quiet ``fresh`` or ``none``.
    """
    if last_commit_days is None:
        staleness = "unknown"
    elif last_commit_days >= STALE_DAYS:
        staleness = "stale"
    else:
        staleness = "aging" if last_commit_days >= AGING_DAYS else "fresh"
    if drifted_share is None:
        return staleness, "unknown"
    if removed_count > 0 or (drifted_count >= HEAVY_MIN_FILES and drifted_share >= HEAVY_SHARE):
        return staleness, "heavy"
    return staleness, "light" if drifted_count > 0 else "none"


def _run(args: list[str], timeout: int) -> tuple[int, str, str]:
    """Run ``gh``; return ``(exit code, stdout, stderr)``, with exit code ``-1`` on timeout or a missing binary."""
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603, S607
    except (OSError, subprocess.TimeoutExpired):
        return -1, "", ""
    return proc.returncode, proc.stdout, proc.stderr


def _gh(args: list[str], timeout: int) -> str | None:
    """Return ``gh`` stdout, or ``None`` on any failure."""
    code, out, _ = _run(args, timeout)
    return out if code == 0 else None


def _days_since(iso: str | None) -> int | None:
    """Whole days between an ISO-8601 UTC timestamp and now, or ``None`` when it cannot be parsed."""
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, (datetime.now(timezone.utc) - then).days)


def _rows(out: str | None) -> list[list[str]] | None:
    """Split tab-separated ``gh --jq`` output into rows; ``None`` when the call failed."""
    return None if out is None else [line.split("\t") for line in out.splitlines() if line]


def _last_commit_date(slug: str, pr: dict, timeout: int) -> str | None:
    """Date of the PR's head commit, from the commit itself; the PR's own commit list is the fallback."""
    out = _gh(["api", f"repos/{slug}/commits/{pr['headRefOid']}", "--jq", ".commit.committer.date"], timeout)
    if out and out.strip():
        return out.strip()
    commits = pr.get("commits") or []
    return commits[-1].get("committedDate") if commits else None


def _compare_head(slug: str, base: str, head: str, timeout: int) -> tuple[int, int, str, str] | None:
    """Return ``(ahead_by, behind_by, merge_base_sha, merge_base_date)`` of head against base, or ``None``."""
    jq = "[.ahead_by,.behind_by,.merge_base_commit.sha,.merge_base_commit.commit.committer.date]|@tsv"
    rows = _rows(_gh(["api", f"repos/{slug}/compare/{base}...{head}", "--jq", jq], timeout))
    if not rows or len(rows[0]) != 4 or not (rows[0][0].isdigit() and rows[0][1].isdigit()):
        return None
    return int(rows[0][0]), int(rows[0][1]), rows[0][2], rows[0][3]


def _pr_files(slug: str, number: int, timeout: int) -> list[tuple[str, int]] | None:
    """Every file the PR changes with its changed-line count, paged; ``None`` when the call failed."""
    jq = ".[] | [.filename,(.additions+.deletions)] | @tsv"
    rows = _rows(_gh(["api", "--paginate", f"repos/{slug}/pulls/{number}/files", "--jq", jq], timeout))
    if rows is None:
        return None
    return [(r[0], int(r[1])) for r in rows if len(r) == 2 and r[1].isdigit()]


def _base_changes(slug: str, merge_base: str, base: str, timeout: int) -> list[tuple[str, str, str, int]] | None:
    """Files the base changed since the merge base as ``(name, status, previous_name, lines)``.

    Returns ``None`` when the call failed or the list reaches the 300-file cap and may be cut off.
    """
    jq = '.files[]? | [.filename,.status,(.previous_filename // ""),(.additions+.deletions)] | @tsv'
    rows = _rows(_gh(["api", f"repos/{slug}/compare/{merge_base}...{base}", "--jq", jq], timeout))
    if rows is None or len(rows) >= COMPARE_FILE_CAP:
        return None
    return [(r[0], r[1], r[2], int(r[3])) for r in rows if len(r) == 4 and r[3].isdigit()]


def _from_compare(files: list[tuple[str, int]], changes: list[tuple[str, str, str, int]]) -> list[dict]:
    """Drifted entries for the PR's files from the base's change list; a base rename is ``renamed``, not removed."""
    by_name = {name: (status, lines) for name, status, _, lines in changes}
    moved = {prev: (name, lines) for name, status, prev, lines in changes if status == "renamed" and prev}
    out: list[dict] = []
    for path, _ in files:
        if path in moved:
            out.append({"path": path, "kind": "renamed", "renamed_to": moved[path][0], "weight": moved[path][1]})
        elif path in by_name:
            status, lines = by_name[path]
            kind = "removed" if status == "removed" else "changed"
            out.append({"path": path, "kind": kind, "renamed_to": None, "weight": lines})
    return out


def _probe_file(slug: str, base: str, merge_base: tuple[str, str], path: str, timeout: int) -> dict | None:
    """Per-file fallback: base commits touching ``path`` after the merge base, and whether the path still exists.

    The merge-base commit is excluded (``since`` is inclusive). Only a 404 from the contents API means the path is gone,
    and then ``absent`` — the base may have moved the file rather than deleted it. Any other error is a failed probe.
    """
    sha, date = merge_base
    jq = ".[].sha"
    out = _gh(["api", "-X", "GET", f"repos/{slug}/commits", "-f", f"sha={base}", "-f", f"path={path}", "-f", f"since={date}",
               "-f", "per_page=100", "--jq", jq], timeout)  # fmt: skip
    if out is None:
        return None
    count = sum(1 for line in out.split() if line != sha)
    if not count:
        return {"path": path, "kind": "unchanged", "renamed_to": None, "weight": 0}
    code, _, err = _run(["api", "-X", "GET", f"repos/{slug}/contents/{quote(path)}", "-f", f"ref={base}"], timeout)
    if code != 0 and "404" not in err and "Not Found" not in err:
        return None
    kind = "absent" if code != 0 else "changed"
    return {"path": path, "kind": kind, "renamed_to": None, "weight": count}


def _recent_base_commits(
    slug: str, base: str, merge_base: tuple[str, str], drifted: list[dict], timeout: int
) -> list[dict]:
    """Up to three recent base commits for each of the most drifted files, so the verdict never builds a shell
    command."""
    sha, date = merge_base
    top = sorted(drifted, key=lambda e: -e["weight"])[:RECENT_FILES]
    jq = '.[] | [.sha[0:9],.commit.committer.date,(.commit.message | split("\\n")[0])] | @tsv'

    def fetch(entry: dict) -> list[dict]:
        path = entry["renamed_to"] or entry["path"]
        rows = _rows(_gh(["api", "-X", "GET", f"repos/{slug}/commits", "-f", f"sha={base}", "-f", f"path={path}",
                          "-f", f"since={date}", "-f", "per_page=4", "--jq", jq], timeout)) or []  # fmt: skip
        return [{"path": entry["path"], "sha": r[0], "date": r[1], "subject": r[2]}
                for r in rows if len(r) == 3 and not sha.startswith(r[0])][:3]  # fmt: skip

    with ThreadPoolExecutor(max_workers=5) as pool:
        return [item for group in pool.map(fetch, top) for item in group]


def _repo_slug(timeout: int) -> str:
    """``owner/repo`` of the current repository, empty when it cannot be resolved."""
    return (_gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], timeout) or "").strip()


def _drift(slug: str, number: int, base: str, merge_base: tuple[str, str], max_files: int, timeout: int) -> dict:
    """Measure which of the PR's files the base changed; returns the drift fields and any ``incomplete`` notes."""
    notes: list[str] = []
    files = _pr_files(slug, number, timeout)
    if files is None:
        return {"source": "none", "notes": ["PR file list unavailable: drift not measured"]}
    changes = _base_changes(slug, merge_base[0], base, timeout)
    if changes is not None:
        entries, source, considered = _from_compare(files, changes), "compare", len(files)
    else:
        notes.append(
            "base change list unavailable or cut off: per-file probes by date used (a moved file shows as absent)"
        )
        probed = sorted(files, key=lambda f: -f[1])[:max_files]
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda f: _probe_file(slug, base, merge_base, f[0], timeout), probed))
        good = [r for r in rows if r is not None]
        if len(good) < len(rows):
            notes.append(f"{len(rows) - len(good)} file probe(s) failed")
        if len(files) > len(probed):
            notes.append(f"only the {len(probed)} largest of {len(files)} files probed")
        entries, source, considered = [r for r in good if r["kind"] != "unchanged"], "per-file", len(good)
    share = round(len(entries) / considered, 2) if considered else None
    entries.sort(key=lambda e: -e["weight"])
    recent = _recent_base_commits(slug, base, merge_base, entries, timeout) if entries else []
    return {"source": source, "files": files, "considered": considered, "entries": entries, "share": share,
            "recent": recent, "notes": notes}  # fmt: skip


def measure(number: int, repo: str | None, max_files: int, timeout: int) -> dict:
    """Collect the drift measurements for one PR; raise ``RuntimeError`` when the PR itself cannot be read."""
    scope = ["-R", repo] if repo else []
    fields = "number,url,state,baseRefName,headRefOid,createdAt,mergeable,changedFiles,commits,closingIssuesReferences"
    raw = _gh(["pr", "view", str(number), *scope, "--json", fields], timeout)
    if raw is None:
        raise RuntimeError(f"cannot fetch PR #{number}")
    pr = json.loads(raw)
    result: dict = {
        "number": number, "url": pr.get("url"), "base": pr["baseRefName"], "head_sha": pr["headRefOid"],
        "state": pr["state"], "mergeable": pr.get("mergeable"), "age_days": _days_since(pr.get("createdAt")),
        "last_commit_days": None, "merge_base_age_days": None, "ahead_by": None, "behind_by": None,
        "files_total": pr.get("changedFiles"), "files_considered": 0, "source": "none", "drifted_files": [],
        "drifted_share": None, "removed_count": 0, "recent_base_commits": [],
        "linked_issues": [i["url"] for i in pr.get("closingIssuesReferences") or [] if i.get("url")],
        "incomplete": [],
    }  # fmt: skip
    if pr["state"] != "OPEN":
        result.update(staleness="n/a", drift="n/a")
        return result
    slug = repo or _repo_slug(timeout)
    if not slug:
        result["incomplete"].append("repository unknown: drift not measured")
    else:
        result["last_commit_days"] = _days_since(_last_commit_date(slug, pr, timeout))
        cmp_head = _compare_head(slug, pr["baseRefName"], pr["headRefOid"], timeout)
        if cmp_head is None:
            result["incomplete"].append("compare unavailable: behind_by and file drift unknown")
        else:
            result["ahead_by"], result["behind_by"] = cmp_head[0], cmp_head[1]
            result["merge_base_age_days"] = _days_since(cmp_head[3])
            found = _drift(slug, number, pr["baseRefName"], (cmp_head[2], cmp_head[3]), max_files, timeout)
            result["incomplete"] += found["notes"]
            result["source"] = found["source"]
            if found["source"] != "none":
                result.update(files_considered=found["considered"], drifted_files=found["entries"],
                              drifted_share=found["share"], recent_base_commits=found["recent"])  # fmt: skip
                if len(found["files"]) < (pr.get("changedFiles") or 0):
                    result["incomplete"].append(
                        f"PR file list cut: {len(found['files'])} of {pr['changedFiles']} files"
                    )
    if result["last_commit_days"] is None:
        result["incomplete"].append("last commit date unavailable")
    result["removed_count"] = sum(1 for e in result["drifted_files"] if e["kind"] in ("removed", "absent"))
    result["staleness"], result["drift"] = grade(
        result["last_commit_days"], result["drifted_share"], len(result["drifted_files"]), result["removed_count"]
    )
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entry: print the drift JSON for one PR."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--number", type=int, required=True)
    parser.add_argument("--repo", help="owner/repo; default: current repository")
    parser.add_argument("--max-files", type=int, default=40, help="per-file fallback only: files probed, largest first")
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args(argv)
    if which("gh") is None:
        print("! gh not found", file=sys.stderr)
        return 1
    try:
        print(json.dumps(measure(args.number, args.repo, args.max_files, args.timeout)))
    except (RuntimeError, json.JSONDecodeError, KeyError) as exc:
        print(f"! {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
