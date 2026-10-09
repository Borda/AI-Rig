#!/usr/bin/env python
"""assemble_vitality_data.py — merge the Group 1 dataset files into the vitality DATA_FILE.

oss:gh-scraper fetches GitHub data in two groups. Group 1
(``fetch_gh_data_group1.py``) writes one JSON file per dataset into a directory;
Group 2 (``fetch_gh_data_group2.py``) appends text-content records straight to the
JSONL ``DATA_FILE``. This script is the deterministic Step 4 that turns the Group 1
directory into JSONL records, keeps the records Group 2 already appended, and checks
the result against the expected dataset list so a missing dataset is reported instead
of silently scored as "no data".

Record rules (``skills/_shared/vitality-data-schema.md`` is the contract):

- One line per dataset: ``{"type", "repo", "timestamp", "records", "partial", "data"}``.
- A zero-byte Group 1 file means the fetch failed (``fetch_gh_data_group1.py`` writes
  ``""`` on any non-zero ``gh`` exit). A valid empty array (``[]``) is real data with
  ``records: 0`` — zero open issues or zero releases is a signal, not a gap.
- Alert datasets (Dependabot, secret scanning) need push access, so a failed fetch is
  recorded as ``"data": "403"``. This is the contract, not a verified HTTP status: the
  Group 1 file does not keep the status code.
- Contributor stats answer HTTP 202 while GitHub computes them; a failed, ``[]`` or
  ``{}`` payload is recorded as ``"data": null, "partial": true, "202_pending": true``.
- ``partial: true`` when a list reaches the item cap its fetch requested (the cap is
  target + 1, so hitting it proves truncation). A list fetched through GitHub search
  stops at the API's ``SEARCH_RESULT_CAP`` ceiling, so a full list of that size is read
  as truncated even though it cannot prove it. The workflow registry object is partial when
  GitHub's ``total_count`` exceeds the entries its one page returned.

Re-running is idempotent: Group 1 record types are always rebuilt from the directory,
every other record type is kept (last occurrence wins, so duplicate appends collapse),
and the file is replaced atomically.

Stdout is a single JSON line that oss:gh-scraper returns verbatim as its envelope:
``{"status", "file", "datasets", "partial", "missing_required", "missing_optional",
"confidence"}``. ``status`` is ``"partial"`` whenever a required dataset is missing.

Usage:
    assemble_vitality_data.py --data-file <path> --group1-dir <dir> --repo <owner/repo>
                              [--analysis-now <epoch-seconds>]

Exit: 0 when DATA_FILE was written (inspect ``status`` for coverage); 1 when the Group 1
directory is missing or DATA_FILE cannot be written; 2 on invalid arguments (argparse).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

#: Same owner/repo shape ``fetch_gh_data_group1.py`` enforces.
_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")

#: Placeholder written for alert datasets whose fetch failed (push access required).
FORBIDDEN_MARKER = "403"

#: Most results GitHub search returns for one query; ``gh issue list --search`` stops there whatever ``--limit`` asks,
#: so a search-backed list of this size may be truncated.
SEARCH_RESULT_CAP = 1000


class DatasetKind(str, Enum):
    """How a failed or empty Group 1 fetch is recorded."""

    STANDARD = "standard"
    ALERTS_403 = "alerts_403"
    STATS_202 = "stats_202"


@dataclass(frozen=True)
class Group1Dataset:
    """One dataset written by ``fetch_gh_data_group1.py``.

    Attributes:
        name: Dataset name; also the ``<name>.json`` file stem and the JSONL ``type``.
        required: Scoring depends on it; a missing required dataset makes the run partial.
        cap: Item count at which the list is truncated (the fetch limit), or ``None``.
        kind: How a failed or empty fetch is recorded.
    """

    name: str
    required: bool
    cap: int | None = None
    kind: DatasetKind = DatasetKind.STANDARD


#: Every Group 1 dataset in output order. Caps mirror the ``--limit``/``per_page`` values in
#: ``fetch_gh_data_group1.py``; datasets without a cap are fixed-size samples by design
#: (``releases`` 10, ``commits_50`` 50, GraphQL ``first:N`` queries) or uncapped listings.
#: Optional: alert datasets (always yield a record, data or "403"), and datasets no axis
#: rubric reads today (``fork_dates``, ``all_issues``, ``all_prs``, ``discussions`` — the
#: last also fails legitimately when Discussions are disabled). ``closed_issues`` covers the
#: 30-day closing window through GitHub search, so its cap is the search ceiling. The workflow
#: registry (``ci_workflows``) is one page of 100 entries beside GitHub's ``total_count``, which
#: decides its truncation exactly (see :func:`_truncated`). CI runs are a Group 2 record
#: (``ci_runs``, scoped to the default branch): one page of 100 runs is never flagged as a
#: truncation — it holds the newest 20 counted runs unless more than 80 of them are excluded
#: (``runs_sampled`` below 20 with ``runs_fetched`` 100), a short sample the scorer notes.
GROUP1_DATASETS: tuple[Group1Dataset, ...] = (
    Group1Dataset("open_issues", required=True, cap=501),
    Group1Dataset("closed_issues", required=True, cap=SEARCH_RESULT_CAP),
    Group1Dataset("open_prs", required=True, cap=201),
    Group1Dataset("closed_prs", required=True, cap=201),
    Group1Dataset("commits", required=True, cap=100),
    Group1Dataset("releases", required=True),
    Group1Dataset("contributor_stats", required=True, kind=DatasetKind.STATS_202),
    Group1Dataset("repo_metadata", required=True),
    Group1Dataset("ci_workflows", required=True, cap=100),
    Group1Dataset("dependabot_alerts", required=False, cap=100, kind=DatasetKind.ALERTS_403),
    Group1Dataset("secret_scanning_alerts", required=False, cap=30, kind=DatasetKind.ALERTS_403),
    Group1Dataset("fork_dates", required=False, cap=100),
    Group1Dataset("merged_prs_90d", required=True, cap=201),
    Group1Dataset("commits_50", required=True),
    Group1Dataset("responsiveness_gql", required=True),
    Group1Dataset("review_coverage_gql", required=True),
    Group1Dataset("root_contents", required=True),
    Group1Dataset("all_issues", required=False, cap=200),
    Group1Dataset("all_prs", required=False, cap=100),
    Group1Dataset("discussions", required=False),
)

_GROUP1_NAMES = frozenset(spec.name for spec in GROUP1_DATASETS)


@dataclass(frozen=True)
class Listings:
    """Lower-cased file names proving which Group 2 content exists in the repository.

    Attributes:
        root: Names from the ``root_contents`` record; ``None`` when that record is missing.
        github_dir: Names from the ``github_dir`` record; ``None`` when that record is missing.
        docs_dir: Names from the ``docs_dir`` record; ``None`` when that record is missing.
        workflows: Names from the ``workflows_list`` record (``.github/workflows/``); ``None`` when it is missing.
    """

    root: frozenset[str] | None
    github_dir: frozenset[str] | None
    docs_dir: frozenset[str] | None = None
    workflows: frozenset[str] | None = None


def _in_root(listings: Listings, *names: str) -> bool:
    """Tell whether any of ``names`` (lower-case) appears in the repository root listing."""
    return listings.root is not None and any(name in listings.root for name in names)


def _in_github_dir(listings: Listings, *names: str) -> bool:
    """Tell whether any of ``names`` (lower-case) appears in the ``.github/`` listing."""
    return listings.github_dir is not None and any(name in listings.github_dir for name in names)


def _stem_listed(names: frozenset[str] | None, *stems: str) -> bool:
    """Tell whether a listing holds a file whose lower-case stem (text before the first dot) is one of ``stems``.

    Examples:
        >>> _stem_listed(frozenset({"readme.md", "src"}), "readme")
        True
        >>> _stem_listed(None, "readme")
        False
    """
    return names is not None and any(name.split(".", 1)[0] in stems for name in names)


def _policy_listed(listings: Listings, stem: str) -> bool:
    """Tell whether a known listing (root, ``.github/``, ``docs/``) holds a policy file such as ``SECURITY.md``.

    Only names with an extension count (``CONTRIBUTING.rst`` does): a bare ``security`` or ``contributing`` entry is
    usually a directory, which Group 2 cannot fetch as a policy file.

    Examples:
        >>> _policy_listed(Listings(root=frozenset({"security"}), github_dir=frozenset({"security.md"})), "security")
        True
        >>> _policy_listed(Listings(root=frozenset({"security"}), github_dir=None), "security")
        False
        >>> docs = Listings(root=frozenset({"docs"}), github_dir=None, docs_dir=frozenset({"contributing.rst"}))
        >>> _policy_listed(docs, "contributing")
        True
    """
    return any(
        names is not None and any(name.split(".", 1)[0] == stem and "." in name for name in names)
        for names in (listings.root, listings.github_dir, listings.docs_dir)
    )


def _docs_unlisted(listings: Listings) -> bool:
    """Tell whether the root lists ``docs/`` but its listing is unknown, so a policy file there is undecidable.

    Examples:
        >>> _docs_unlisted(Listings(root=frozenset({"docs"}), github_dir=None))
        True
        >>> _docs_unlisted(Listings(root=frozenset({"docs"}), github_dir=None, docs_dir=frozenset()))
        False
    """
    return listings.docs_dir is None and _in_root(listings, "docs")


def _policy_undecidable(stem: str) -> Callable[[Listings], bool]:
    """Build the ambiguity test for a policy file: not listed anywhere known, while ``docs/`` was never listed."""
    return lambda listings: not _policy_listed(listings, stem) and _docs_unlisted(listings)


def _codeowners_listed(listings: Listings) -> bool:
    """Tell whether a known listing (root, ``.github/``, ``docs/``) holds CODEOWNERS — every place GitHub reads it.

    Examples:
        >>> _codeowners_listed(Listings(root=frozenset({"docs"}), github_dir=None, docs_dir=frozenset({"codeowners"})))
        True
    """
    return any(
        names is not None and "codeowners" in names for names in (listings.root, listings.github_dir, listings.docs_dir)
    )


def _workflow_yaml_listed(listings: Listings) -> bool:
    """Tell whether ``.github/workflows/`` may hold a workflow file to fetch: a ``.yml``/``.yaml`` name is listed.

    The directory alone proves nothing once its listing is known — a directory holding only a README runs nothing and
    Group 2 fetches no content from it; an unknown listing keeps the content expected.

    Examples:
        >>> github = frozenset({"workflows"})
        >>> _workflow_yaml_listed(Listings(root=None, github_dir=github, workflows=frozenset({"readme.md"})))
        False
        >>> _workflow_yaml_listed(Listings(root=None, github_dir=github, workflows=None))
        True
    """
    if not _in_github_dir(listings, "workflows"):
        return False
    return listings.workflows is None or any(name.endswith((".yml", ".yaml")) for name in listings.workflows)


#: File-name stems of a changelog; ``fetch_gh_data_group2.py`` tries them in this order.
CHANGELOG_STEMS = ("changelog", "changes", "history", "news")
#: ``.github/`` names of a Dependabot configuration file (GitHub accepts both extensions).
DEPENDABOT_CONFIG_NAMES = ("dependabot.yml", "dependabot.yaml")


def is_changelog_file(name: str) -> bool:
    """Tell whether a lower-case root entry is certainly a changelog file.

    Only names with an extension count. A bare ``changelog``/``changes``/``history``/``news`` entry may be a
    fragment directory (towncrier) or an extension-less file (GNU ``ChangeLog``/``NEWS``); the listing cannot tell
    which, so :func:`is_changelog_entry` covers those.

    Examples:
        >>> is_changelog_file("changelog.md"), is_changelog_file("changes"), is_changelog_file("changelog")
        (True, False, False)
    """
    return "." in name and name.split(".", 1)[0] in CHANGELOG_STEMS


def is_changelog_entry(name: str) -> bool:
    """Tell whether a lower-case root entry may hold the changelog: a changelog file or a bare changelog-like name.

    Examples:
        >>> is_changelog_entry("changes"), is_changelog_entry("news.rst"), is_changelog_entry("src")
        (True, True, False)
    """
    return name.split(".", 1)[0] in CHANGELOG_STEMS


def _changelog_listed(listings: Listings) -> bool:
    """Tell whether the root listing has a changelog file (see :func:`is_changelog_file`).

    Examples:
        >>> _changelog_listed(Listings(root=frozenset({"changes"}), github_dir=None))
        False
        >>> _changelog_listed(Listings(root=frozenset({"changelog.md"}), github_dir=None))
        True
    """
    return listings.root is not None and any(is_changelog_file(name) for name in listings.root)


def _changelog_ambiguous(listings: Listings) -> bool:
    """Tell whether the root lists only bare changelog-like names, which may be files or directories.

    Examples:
        >>> _changelog_ambiguous(Listings(root=frozenset({"changes", "src"}), github_dir=None))
        True
        >>> _changelog_ambiguous(Listings(root=frozenset({"changes", "changes.md"}), github_dir=None))
        False
    """
    if listings.root is None or _changelog_listed(listings):
        return False
    return any(is_changelog_entry(name) for name in listings.root)


#: Group 2 record types with the listing evidence that makes each one expected. Group 2
#: swallows 404s, so absence is normal when the file does not exist; absence while the
#: listing shows the file means the fetch failed. ``branch_protection`` is never
#: required: an unprotected branch (404) and a token without admin rights (403) both
#: leave it absent and cannot be told apart — ``default_branch_status`` carries the
#: protection flag instead. That record and ``ci_runs`` (an empty list for a repository
#: without runs) come from endpoints every run reaches, so both are always required: their
#: absence means Group 2 did not run, used a wrong branch, or the fetch failed.
GROUP2_EVIDENCE: tuple[tuple[str, Callable[[Listings], bool]], ...] = (
    ("readme_content", lambda listings: _stem_listed(listings.root, "readme")),
    ("contributing_text", lambda listings: _policy_listed(listings, "contributing")),
    ("security_text", lambda listings: _policy_listed(listings, "security")),
    ("changelog_headings", _changelog_listed),
    ("github_dir", lambda listings: _in_root(listings, ".github")),
    ("docs_dir", lambda listings: _in_root(listings, "docs")),
    ("codeowners_text", _codeowners_listed),
    ("workflows_list", lambda listings: _in_github_dir(listings, "workflows")),
    ("workflow_files", _workflow_yaml_listed),
    ("dependabot_config", lambda listings: _in_github_dir(listings, *DEPENDABOT_CONFIG_NAMES)),
    ("ci_runs", lambda _listings: True),
    ("default_branch_status", lambda _listings: True),
)

#: Group 2 record types whose listing evidence can be inconclusive: the file is not certainly listed, yet the
#: listing does not rule it out either. Absence of such a record is neither a required gap nor proof the file is
#: missing. A bare ``changes`` entry may be a towncrier fragment directory or an extension-less changelog file; a
#: CONTRIBUTING, SECURITY or CODEOWNERS file may sit in a ``docs/`` directory whose listing was not fetched.
GROUP2_AMBIGUOUS: tuple[tuple[str, Callable[[Listings], bool]], ...] = (
    ("changelog_headings", _changelog_ambiguous),
    ("contributing_text", _policy_undecidable("contributing")),
    ("security_text", _policy_undecidable("security")),
    ("codeowners_text", lambda listings: not _codeowners_listed(listings) and _docs_unlisted(listings)),
)


@dataclass(frozen=True)
class Coverage:
    """Dataset coverage of an assembled DATA_FILE.

    Attributes:
        datasets: Number of records written.
        partial: Record types flagged ``partial: true``.
        missing_required: Required datasets with no record.
        missing_optional: Optional Group 1 datasets whose fetch failed.
        required_partial: How many required datasets are partial (drives confidence).
    """

    datasets: int
    partial: list[str]
    missing_required: list[str]
    missing_optional: list[str]
    required_partial: int


def _record_count(data: Any) -> int | None:
    """Count the items a record carries.

    Args:
        data: Parsed dataset payload.

    Returns:
        List length; ``0`` for the ``"403"`` marker; ``None`` for objects and ``null``.

    Examples:
        >>> _record_count([1, 2, 3])
        3
        >>> _record_count("403")
        0
        >>> _record_count({"count": 2}) is None
        True
    """
    if isinstance(data, list):
        return len(data)
    if data == FORBIDDEN_MARKER:
        return 0
    return None


def _read_group1_payload(path: Path) -> tuple[bool, Any]:
    """Read one Group 1 file and say whether the fetch succeeded.

    Args:
        path: ``<group1-dir>/<name>.json``.

    Returns:
        ``(True, parsed_json)`` on success; ``(False, None)`` when the file is absent,
        zero-byte (failed fetch) or not valid JSON.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, None
    if not text.strip():
        return False, None
    try:
        return True, json.loads(text)
    except json.JSONDecodeError:
        print(f"[assemble_vitality_data] WARN: {path.name} is not valid JSON — treated as failed", file=sys.stderr)
        return False, None


def build_group1_record(spec: Group1Dataset, ok: bool, payload: Any, base: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one Group 1 fetch result into a JSONL record.

    Args:
        spec: Dataset definition.
        ok: Whether the fetch produced valid JSON.
        payload: Parsed payload when ``ok``.
        base: ``{"repo": ..., "timestamp": ...}`` shared by every record.

    Returns:
        The record, or ``None`` when the dataset is missing (failed standard fetch).

    Examples:
        >>> base = {"repo": "o/r", "timestamp": 1}
        >>> spec = Group1Dataset("open_issues", required=True, cap=2)
        >>> build_group1_record(spec, True, [1, 2], base)["partial"]
        True
        >>> build_group1_record(spec, False, None, base) is None
        True
        >>> alerts = Group1Dataset("dependabot_alerts", required=False, kind=DatasetKind.ALERTS_403)
        >>> build_group1_record(alerts, False, None, base)["data"]
        '403'
    """
    record: dict[str, Any] = {"type": spec.name, **base}
    if spec.kind is DatasetKind.STATS_202 and (not ok or payload in ([], {})):
        record.update({"records": None, "partial": True, "202_pending": True, "data": None})
        return record
    if not ok:
        if spec.kind is DatasetKind.ALERTS_403:
            record.update({"records": 0, "partial": False, "data": FORBIDDEN_MARKER})
            return record
        return None
    record.update({"records": _record_count(payload), "partial": _truncated(spec, payload), "data": payload})
    return record


def _truncated(spec: Group1Dataset, payload: Any) -> bool:
    """Tell whether a fetched dataset stopped short of everything GitHub holds.

    A list is truncated once it reaches the fetch cap. A registry object (``ci_workflows``) carries GitHub's
    ``total_count`` beside the ``count`` of entries its one page returned, so a shortfall is known exactly; without
    ``total_count`` (older DATA_FILEs) a ``count`` at the cap is read as truncated, as a list would be.

    Examples:
        >>> registry = Group1Dataset("ci_workflows", required=True, cap=100)
        >>> _truncated(registry, {"count": 100, "total_count": 130})
        True
        >>> _truncated(registry, {"count": 100, "total_count": 100})
        False
        >>> _truncated(registry, {"count": 29})
        False
        >>> _truncated(Group1Dataset("open_issues", required=True, cap=2), [1, 2])
        True
    """
    if isinstance(payload, list):
        return spec.cap is not None and len(payload) >= spec.cap
    if not isinstance(payload, dict):
        return False
    count, total = payload.get("count"), payload.get("total_count")
    if isinstance(count, int) and isinstance(total, int):
        return total > count
    return spec.cap is not None and isinstance(count, int) and count >= spec.cap


def read_existing_records(data_file: Path) -> list[dict[str, Any]]:
    """Load the non-Group-1 records already in DATA_FILE, last occurrence per type.

    Group 1 types are dropped because the caller rebuilds them. Blank lines are skipped;
    malformed lines and lines without a ``type`` are dropped with a warning, since a
    scorer cannot use them either.

    Args:
        data_file: Existing JSONL file; absent file yields an empty list.

    Returns:
        Records in first-seen order of their type, each holding its last occurrence.
    """
    try:
        lines = data_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    kept: dict[str, dict[str, Any]] = {}
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            print(f"[assemble_vitality_data] WARN: dropped malformed line {lineno}", file=sys.stderr)
            continue
        rtype = record.get("type") if isinstance(record, dict) else None
        if not isinstance(rtype, str):
            print(f"[assemble_vitality_data] WARN: dropped line {lineno} without a type", file=sys.stderr)
            continue
        if rtype not in _GROUP1_NAMES:
            kept[rtype] = record
    return list(kept.values())


def _names_of(record: dict[str, Any] | None) -> frozenset[str] | None:
    """Lower-case the file names a listing record carries, or ``None`` without a usable list."""
    if record is None or not isinstance(record.get("data"), list):
        return None
    return frozenset(str(name).lower() for name in record["data"])


def listings_of(records: dict[str, dict[str, Any]]) -> Listings:
    """Build the listing evidence (root, ``.github/``, ``docs/``, ``.github/workflows/``) from records keyed by type.

    Examples:
        >>> found = listings_of({"root_contents": {"data": ["README.md", "docs"]}, "docs_dir": {"data": ["Index.md"]}})
        >>> sorted(found.root), found.github_dir, found.docs_dir, found.workflows
        (['docs', 'readme.md'], None, frozenset({'index.md'}), None)
    """
    return Listings(
        root=_names_of(records.get("root_contents")),
        github_dir=_names_of(records.get("github_dir")),
        docs_dir=_names_of(records.get("docs_dir")),
        workflows=_names_of(records.get("workflows_list")),
    )


def compute_coverage(records: list[dict[str, Any]], missing_group1: list[Group1Dataset]) -> Coverage:
    """Check assembled records against the expected dataset list.

    Args:
        records: Every record about to be written.
        missing_group1: Group 1 datasets that produced no record.

    Returns:
        Coverage summary.
    """
    by_type = {record["type"]: record for record in records}
    listings = listings_of(by_type)
    missing_required = [spec.name for spec in missing_group1 if spec.required]
    missing_required += [name for name, expected in GROUP2_EVIDENCE if name not in by_type and expected(listings)]
    required_names = {spec.name for spec in GROUP1_DATASETS if spec.required}
    partial = [record["type"] for record in records if record.get("partial") is True]
    return Coverage(
        datasets=len(records),
        partial=partial,
        missing_required=missing_required,
        missing_optional=[spec.name for spec in missing_group1 if not spec.required],
        required_partial=sum(1 for name in partial if name in required_names),
    )


def coverage_confidence(coverage: Coverage) -> float:
    """Score how far scorers can trust the assembled file.

    Truncated required datasets lower confidence as before (0.95 / 0.88 / 0.78). Any missing
    required dataset caps it at 0.70 and takes 0.05 per further missing dataset, floor 0.40,
    so a file with gaps can never report the 0.95 of a complete one.

    Args:
        coverage: Coverage summary.

    Returns:
        Confidence in ``[0.40, 0.95]``.

    Examples:
        >>> coverage_confidence(Coverage(26, [], [], [], 0))
        0.95
        >>> coverage_confidence(Coverage(25, [], ["review_coverage_gql"], [], 0))
        0.7
        >>> coverage_confidence(Coverage(20, [], ["a", "b", "c"], [], 3))
        0.6
    """
    if coverage.required_partial == 0:
        confidence = 0.95
    elif coverage.required_partial <= 2:
        confidence = 0.88
    else:
        confidence = 0.78
    if coverage.missing_required:
        confidence = min(confidence, 0.70) - 0.05 * (len(coverage.missing_required) - 1)
    return round(max(confidence, 0.40), 2)


def _write_atomic(data_file: Path, records: list[dict[str, Any]]) -> None:
    """Replace ``data_file`` with ``records`` as JSONL in one rename.

    Args:
        data_file: Destination path; its parent is created when absent.
        records: Records written one per line, LF-terminated on every OS.
    """
    data_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{data_file.name}.", suffix=".tmp", dir=data_file.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False))
                handle.write("\n")
        os.replace(tmp_path, data_file)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def assemble(data_file: Path, group1_dir: Path, repo: str, analysis_now: int) -> Coverage:
    """Rebuild DATA_FILE from the Group 1 directory plus the records already in it.

    Args:
        data_file: JSONL path Group 2 appended to; replaced atomically.
        group1_dir: Directory of ``<dataset>.json`` files from ``fetch_gh_data_group1.py``.
        repo: ``owner/repo`` slug stamped on every Group 1 record.
        analysis_now: Epoch seconds stamped as ``timestamp``.

    Returns:
        Coverage of the written file.
    """
    base = {"repo": repo, "timestamp": analysis_now}
    group1_records: list[dict[str, Any]] = []
    missing: list[Group1Dataset] = []
    for spec in GROUP1_DATASETS:
        ok, payload = _read_group1_payload(group1_dir / f"{spec.name}.json")
        record = build_group1_record(spec, ok, payload, base)
        if record is None:
            missing.append(spec)
        else:
            group1_records.append(record)
    records = group1_records + read_existing_records(data_file)
    _write_atomic(data_file, records)
    return compute_coverage(records, missing)


def _envelope(data_file: Path, coverage: Coverage) -> dict[str, Any]:
    """Build the oss:gh-scraper return envelope from the coverage summary."""
    return {
        "status": "partial" if coverage.missing_required else "done",
        "file": data_file.as_posix(),
        "datasets": coverage.datasets,
        "partial": coverage.partial,
        "missing_required": coverage.missing_required,
        "missing_optional": coverage.missing_optional,
        "confidence": coverage_confidence(coverage),
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse and validate the command line."""
    parser = argparse.ArgumentParser(
        prog="assemble_vitality_data.py",
        description="Merge Group 1 dataset files into the vitality DATA_FILE and report dataset coverage.",
    )
    parser.add_argument("--data-file", required=True, help="JSONL DATA_FILE; Group 2 records in it are kept.")
    parser.add_argument("--group1-dir", required=True, help="Directory written by fetch_gh_data_group1.py.")
    parser.add_argument("--repo", required=True, help="owner/repo slug stamped on each record.")
    parser.add_argument(
        "--analysis-now",
        type=int,
        default=None,
        help="Epoch seconds stamped as each record's timestamp (default: now).",
    )
    args = parser.parse_args(argv)
    if not _REPO_RE.match(args.repo):
        parser.error(f"--repo must match 'owner/repo' (allowed chars: A-Za-z0-9._-), got: {args.repo!r}")
    return args


def main(argv: list[str] | None = None) -> int:
    """Assemble DATA_FILE and print the coverage envelope.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when DATA_FILE was written; ``1`` when the Group 1 directory is missing or
        the write failed.
    """
    args = _parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    group1_dir = Path(args.group1_dir)
    if not group1_dir.is_dir():
        print(f"[assemble_vitality_data] ERROR: Group 1 directory not found: {group1_dir}", file=sys.stderr)
        return 1
    data_file = Path(args.data_file)
    analysis_now = args.analysis_now if args.analysis_now is not None else int(time.time())
    try:
        coverage = assemble(data_file, group1_dir, args.repo, analysis_now)
    except OSError as exc:
        print(f"[assemble_vitality_data] ERROR: cannot write {data_file}: {exc}", file=sys.stderr)
        return 1
    missing = ", ".join(coverage.missing_required) or "none"
    print(
        f"[assemble_vitality_data] {coverage.datasets} datasets → {data_file}; missing required: {missing}",
        file=sys.stderr,
    )
    print(json.dumps(_envelope(data_file, coverage), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
