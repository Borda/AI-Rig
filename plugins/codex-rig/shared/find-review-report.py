#!/usr/bin/env python3
"""Select assessed review artifacts for explicit local intake or pull-request lookup.

## Purpose

Let remediation locate an assessed prior review report that matches the canonical PR URL rather than guessing from
timestamps alone. This prevents code-remediate from applying findings from a different pull request, a review that never
reached an assessed verdict, or a terminal close disposition.

## Scope

It scans local report directories and JSON identity files without querying GitHub or changing report content. Completion
mode runs both packaged artifact validators before allowing final text to leave the producer boundary. Explicit local
and PR intake reuse that same validation, including source and applicable final-handoff bindings, before returning the
artifact path. Matching accepts a PR number, ``#number``, or canonical GitHub URL and checks ``pr.json`` identity and
result metadata. GitHub scheme, host, owner, and repository casing do not change PR identity; the canonical URL
structure and PR number remain exact.

## Usage

Run ``python find-review-report.py --target <pr-url-or-number>`` to select an assessed report. Alternatively, run the
same script with ``--result <path>`` to accept assessed working-tree, path, commit, or PR reports while rejecting
supplied unavailable, closed, or unpromoted candidate results. The target search covers canonical
``pr-<number>/run-<NNN>`` directories, timestamped flat reports, and, for the default current root, the legacy
``.reports/codex/review`` root. ``--complete-run <run>`` validates the explicitly named promoted review and emits its
bound final Markdown bytes, even when a newer run exists. For standalone PR review entry, add ``--latest-run`` to
``--target <canonical PR URL>`` to locate the current retained producer directory, including unfinished work.
Use ``--latest-run --local-scope <scope>`` for a working tree, path or commit. Path and commit reviews also take
``--local-target <path-or-revision>``. Local matches bind the original checkout, selection, revision and changed bytes.
Confirm the offered directory with ``--expect-run <path>`` before resuming it. Intake still requires a validated result.

## Used by

Review and remediation use this selector to read existing results. Report-only consumers retain the lookup diagnostic.
Only remediation invoked with the explicit +review argument may start or resume a review for that target.

## Outputs

It prints one matching assessed local review-artifact path. With default roots, PR lookup compares the caller's review
runs, reviews stored in its Git administration directory, and existing standalone review archives by allocation time.
These take precedence over the historical review root.
Within one root, the numerically highest PR-scoped run supersedes flat timestamped artifacts. Target lookup requires
PR scope and a
recognized ``metadata.review_decision.recommendation`` in addition to PR identity. Explicit intake accepts all four
declared review scopes without requiring PR identity; current intake artifacts must be canonical schema-3
``result.json`` files that pass both validators. Existing ``--parent-thread-id`` and ``--codex-home`` options identify
evidence from another producer session. Completion and intake default to the recorded producer thread while
revalidating its evidence, so a later consuming session does not replace the original identity.
``--archive-result <path>`` reports a schema-2 or schema-3 result as unverified data and never returns a
remediation-eligible path. ``--finding-evidence <result.json> --finding-id <id>`` prints one admitted finding's
canonical record plus its contained reviewer artifacts (author assessments, ``specialists/`` responses,
``review-notes.md``) for remediation to read when a claim no longer matches source. ``--prior-resolutions <run>``
prints the newest earlier same-PR run's ``resolution.jsonl`` outcomes, latest per finding, for the next review's
reviewer briefs. Both are read-only and print JSON.
Explicit thread overrides remain authoritative. Completion does not broaden target lookup.

## Failure

Absent PR identity, malformed report JSON, a current terminal close, a newer unpromoted candidate, only unavailable
diagnostics, or no matching artifact returns a non-zero status so remediation cannot consume unassessed findings.
Terminal messages distinguish unavailable, closed, unpromoted, incomplete, missing, and invalid candidates. Retained
promoted runs without a result are incomplete evidence, even before notes exist; damaged identity cannot revive an older
verdict. Completion failure emits a blocked-first
diagnostic to stderr and no final text; evidence stays intact for repair.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

# Support installed scripts and callers that load the finder directly from its path.
sys.pycache_prefix = str(Path(__file__).resolve())
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect_diff import diff_sections, local_review_identity, review_file_digest  # noqa: E402
from create_run import PR_URL_PATTERN, TIMESTAMP_RUN_ID, same_pr_url  # noqa: E402
from review_workspace import (  # noqa: E402
    _check_path,
    _git_environment,
    caller_root,
    checkout_report_root,
    historical_workspace,
    report_root_caller,
    workspace_caller,
)

#: Maximum bytes accepted from a previous run's resolution ledger.
MAX_LEDGER_BYTES = 16 * 1024 * 1024


def _read_pr_identity(pr_path: Path) -> tuple[str, str, bool] | None:
    """Read a PR identity without mistaking malformed collector output for a match."""
    try:
        payload: dict[str, Any] = json.loads(pr_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None

    if not isinstance(payload, dict):
        return None
    raw_number = payload.get("number")
    if type(raw_number) is not int or raw_number <= 0:
        return None
    raw_url = payload.get("url", "")
    number = str(raw_number)
    url = raw_url.rstrip("/") if isinstance(raw_url, str) else ""
    match = PR_URL_PATTERN.fullmatch(url) if url else None
    valid = isinstance(raw_url, str) and (not url or (match is not None and match.group("number") == number))
    return number, url, valid


def _matches_target(target: str, number: str, url: str) -> bool:
    """Return whether a canonical PR identity matches a user target string."""
    if not target:
        return False
    numeric_target = target.lstrip("#")
    if numeric_target.isdecimal() and int(numeric_target) > 0 and number == numeric_target:
        return True
    return same_pr_url(target, url)


def review_result_kind(result_path: Path, *, allow_local: bool = False) -> str:
    """Classify review disposition, accepting local scopes only for explicit-path intake."""
    try:
        payload: dict[str, Any] = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return "invalid"
    if not isinstance(payload, dict):
        return "invalid"
    metadata = payload.get("metadata")
    scopes = ("pr", "working-tree", "path", "commit") if allow_local else ("pr",)
    if not isinstance(metadata, dict) or metadata.get("scope") not in scopes:
        return "invalid"
    if metadata.get("review_status") == "unavailable":
        return "unavailable"
    if metadata.get("review_status") == "closed":
        return "closed"
    decision = metadata.get("review_decision")
    if not isinstance(decision, dict):
        return "invalid"
    recommendation = decision.get("recommendation")
    validation = metadata.get("review_validation")
    # source_rating_version 2 reads only the unified tokens; version 1 and unversioned reports keep historical names.
    unified = isinstance(validation, dict) and validation.get("source_rating_version") == 2
    recognized = (
        {"APPROVE", "MINOR_EDITS", "MAJOR_REVISION", "INSUFFICIENT_BUT_PROMISING", "REJECT"}
        if unified
        else {
            "accept-as-is",
            "minor-changes",
            "needs-more-work",
            "insufficient-but-promising",
            "reject",
            "not-aligned",
        }
    )
    if not (
        (isinstance(recommendation, str) and recommendation in recognized)
        or (recommendation is None and isinstance(validation, dict) and validation.get("schema_version") == 1)
    ):
        return "invalid"
    return "assessed"


def require_assessed_review_result(
    result_path: Path,
    *,
    codex_home: Path | None = None,
    parent_thread_id: str | None = None,
    reports_dir: Path | None = None,
) -> Path:
    """Validate assessed artifacts before explicit or discovered report intake.

    Local intake reuses the producer completion boundary and its optional evidence-location and thread overrides. It
    recovers the producer thread from its manifest for every assessed scope when no override is supplied. It returns
    only the selected path, never the producer's final text.
    """
    if result_path.name == CANDIDATE_RESULT_NAME:
        raise LookupError(f"matching-review-candidate-unpromoted:{result_path}")
    kind = review_result_kind(result_path, allow_local=True)
    if kind == "unavailable":
        raise LookupError(f"matching-review-unavailable-rerun-code-review:{result_path}")
    if kind == "closed":
        raise LookupError(f"matching-review-closed-not-remediable:{result_path}")
    if kind != "assessed":
        raise LookupError(f"invalid-review-report-rerun-code-review:{result_path}")
    if result_path.name != "result.json":
        raise LookupError(f"invalid-review-report-rerun-code-review:{result_path}")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 3:
        raise LookupError("historical-review-unverified-archive:use --archive-result for read-only access")
    complete_review_run(result_path.parent, codex_home=codex_home, parent_thread_id=parent_thread_id)
    if payload["metadata"].get("scope") == "pr":
        run_dir = result_path.resolve().parent
        identity = _read_pr_identity(run_dir / "pr.json")
        if identity is None or not identity[2]:
            raise LookupError("review-completion-missing-pr-identity")
        reports_root = run_dir.parent.parent if PR_DIRECTORY_PATTERN.fullmatch(run_dir.parent.name) else run_dir.parent
        target, reports_dirs = _pr_roots_for_directory(identity[1] or identity[0], reports_root)
        if reports_dir is not None and reports_dir.resolve() not in {root.resolve() for root in reports_dirs}:
            raise LookupError("missing-matching-review-report")
        selected = find_latest_review_report(target, reports_dirs)
        if selected.resolve() != result_path.resolve():
            raise LookupError(f"review-completion-not-current:{selected}")
    return result_path


def read_archive_result(result_path: Path) -> dict[str, Any]:
    """Describe old report data without certifying its findings or provenance."""
    if result_path.name != "result.json":
        raise LookupError("historical-review-archive-result-path-invalid")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") not in {2, 3}:
        raise LookupError("historical-review-archive-schema-invalid")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise LookupError("historical-review-archive-metadata-invalid")
    return {
        "status": "unverified-archive",
        "path": str(result_path),
        "schema_version": payload["schema_version"],
        "scope": metadata.get("scope"),
    }


#: Directory searched first for code-review reports, relative to the repository root.
CURRENT_REPORTS_DIR = Path(".reports/codex/code-review")
#: Older review-report directory, searched as a fallback after the current one.
LEGACY_REPORTS_DIR = Path(".reports/codex/review")
#: File name of a not-yet-finalized review result, accepted alongside `result.json`.
CANDIDATE_RESULT_NAME = "result.candidate.json"
#: Pattern for a `pr-N` report directory; the capture group is the pull-request number.
PR_DIRECTORY_PATTERN = re.compile(r"pr-([1-9][0-9]*)")
#: Pattern for a `run-NNN` report directory; the capture group is the run sequence number.
RUN_DIRECTORY_PATTERN = re.compile(r"run-([0-9]{3,})")


def _default_pr_roots(target: str, caller: Path | None = None) -> tuple[str, list[Path]]:
    """Locate the caller’s reviews and retained historical runs without creating report storage."""
    caller = caller or Path.cwd()
    # Legacy report-only directories need no Git checkout. A discovered Git checkout must resolve normally.
    is_checkout = any((parent / ".git").exists() for parent in (caller, *caller.parents))
    if is_checkout:
        caller = caller_root(caller)
    target = target.strip().rstrip("/")
    if is_checkout and target.lstrip("#").isdecimal():
        resolved = subprocess.run(  # noqa: S603 - packaged local Git identity reader
            [
                sys.executable,
                str(Path(__file__).with_name("select-git-remote.py")),
                "--canonical-pr-url",
                target.lstrip("#"),
                "--cwd",
                str(caller),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=_git_environment(),
        )
        if resolved.returncode == 0:
            target = resolved.stdout.strip()
        elif resolved.stderr.strip() not in {
            "no-github-origin",
            "ambiguous-github-origin",
            "no-github-repository",
            "ambiguous-github-repositories",
            "invalid-pr-number",
        }:
            raise LookupError("review-pr-identity-lookup-failed")
    canonical = PR_URL_PATTERN.fullmatch(target)
    if canonical:
        caller = workspace_caller(caller, target) or caller
    roots = [caller / CURRENT_REPORTS_DIR, caller / LEGACY_REPORTS_DIR]
    if is_checkout:
        managed = checkout_report_root(caller)
        if managed is not None and managed.exists():
            _check_path(managed / "code-review", managed)
            roots.insert(0, managed / "code-review")
    if canonical:
        workspace = historical_workspace(caller, target)
        if workspace is not None:
            if workspace_caller(workspace, target) != caller:
                raise LookupError("private review workspace identity mismatch")
            roots.insert(0, workspace / ".reports" / "codex" / "code-review")
    return target, list(dict.fromkeys(roots))


def _pr_roots_for_directory(target: str, root: Path) -> tuple[str, list[Path]]:
    """Expand a managed report root to its peers and keep a custom archive in its own freshness domain."""
    if root.parts[-3:] == ("codex-rig", "reports", "code-review"):
        caller = report_root_caller(root.parent)
        if caller is not None:
            return _default_pr_roots(target, caller)
    if root.parts[-3:] in {(".reports", "codex", "review"), (".reports", "codex", "code-review")}:
        # A copied report hierarchy may be nested under an active checkout without belonging to its workflow.
        if (root.parents[2] / ".git").exists():
            return _default_pr_roots(target, root.parents[2])
        return target, [root]
    if root in {CURRENT_REPORTS_DIR.resolve(), LEGACY_REPORTS_DIR.resolve()}:
        return _default_pr_roots(target)
    return target, [root]


class ReviewArtifact(NamedTuple):
    """Bind a review result path to its topology-aware ordering identity."""

    path: Path
    order: tuple[int, int, str | int]
    pull_number: str | None


def _root_priority(root: Path) -> int:
    """Let either current producer domain supersede the historical review namespace."""
    return int(root.resolve() != LEGACY_REPORTS_DIR.resolve() and root.parts[-3:] != (".reports", "codex", "review"))


def _review_artifacts(reports_dir: Path, *, root_priority: int = 0) -> list[ReviewArtifact]:
    """Enumerate results with their report-root and local-run ordering."""
    artifacts: list[ReviewArtifact] = []
    for report_dir in reports_dir.glob("*"):
        if not report_dir.is_dir():
            continue
        pr_match = PR_DIRECTORY_PATTERN.fullmatch(report_dir.name)
        if pr_match is not None:
            pull_number = pr_match.group(1)
            for run_dir in report_dir.glob("run-*"):
                run_match = RUN_DIRECTORY_PATTERN.fullmatch(run_dir.name)
                if run_match is None or int(run_match.group(1)) < 1 or not run_dir.is_dir():
                    continue
                order = (root_priority, 1, int(run_match.group(1)))
                for result_name in ("result.json", CANDIDATE_RESULT_NAME):
                    result_path = run_dir / result_name
                    if result_path.is_file():
                        artifacts.append(ReviewArtifact(result_path, order, pull_number))
                if not any((run_dir / name).is_file() for name in ("result.json", CANDIDATE_RESULT_NAME)):
                    notes = run_dir / "review-notes.md"
                    if notes.is_file():
                        artifacts.append(ReviewArtifact(notes, order, pull_number))
                    else:
                        # Promotion precedes notes and results; the run itself must block older findings.
                        artifacts.append(ReviewArtifact(run_dir / "pr.json", order, pull_number))
            continue
        if report_dir.name.startswith("pr-"):
            continue
        order = (root_priority, 0, report_dir.name)
        for result_name in ("result.json", CANDIDATE_RESULT_NAME):
            result_path = report_dir / result_name
            if result_path.is_file():
                artifacts.append(ReviewArtifact(result_path, order, None))
        if not any((report_dir / name).is_file() for name in ("result.json", CANDIDATE_RESULT_NAME)):
            notes = report_dir / "review-notes.md"
            if notes.is_file():
                artifacts.append(ReviewArtifact(notes, order, None))
            else:
                # A standalone PR allocation precedes both collector receipts.
                name = TIMESTAMP_RUN_ID.fullmatch(report_dir.name)
                pending_names = ["pr-target.txt", "pr.json"]
                if name and name["pr_number"]:
                    pending_names.append("review-entry.json")
                for pending_name in pending_names:
                    pending = report_dir / pending_name
                    if pending.is_file() or pending.is_symlink():
                        artifacts.append(ReviewArtifact(pending, order, None))
                        break
    return artifacts


def _target_pr_number(target: str) -> str | None:
    """Extract the PR number from an advertised numeric or canonical GitHub target."""
    numeric = target.lstrip("#")
    if numeric.isdecimal() and int(numeric) > 0:
        return str(int(numeric))
    match = PR_URL_PATTERN.fullmatch(target)
    return match.group("number") if match else None


def _artifact_matches_target(
    artifact: ReviewArtifact, target: str, *, allow_target_file: bool = False, allow_unverified_directory: bool = False
) -> bool:
    """Match verified PR identity or conservatively identify a pending run."""
    target_number = _target_pr_number(target)
    directory_matches = (
        allow_unverified_directory and target_number is not None and artifact.pull_number == target_number
    )
    identity = _read_pr_identity(artifact.path.parent / "pr.json")
    if identity is not None:
        number, url, valid = identity
        if artifact.pull_number is not None and artifact.pull_number != number:
            return directory_matches
        if valid:
            if _matches_target(target, number, url):
                return True
            return directory_matches and not url
        if allow_unverified_directory and target_number == number:
            return True
    if directory_matches:
        return True
    if not allow_target_file:
        return False
    target_path = artifact.path.parent / "pr-target.txt"
    try:
        stored_target = target_path.read_text(encoding="utf-8").strip() if target_path.is_file() else ""
    except (OSError, UnicodeError):
        return False
    if _matches_target(target, stored_target, stored_target):
        return True
    return (
        target.lstrip("#").isdecimal()
        and target_number is not None
        and target_number == _target_pr_number(stored_target)
    )


def _entry_object(path: Path) -> dict:
    """Read a retained object without following an evidence symlink or admitting another JSON shape."""
    if path.is_symlink():
        raise LookupError(f"redirected-review-run:{path}")
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise ValueError(f"invalid-review-entry:{path}")
    return data


def _allocated_target_matches(artifact: ReviewArtifact, target: str) -> bool | None:
    """Identify a producer by its allocation even when collection is absent or contradicts the entry."""
    run = artifact.path.parent
    name = TIMESTAMP_RUN_ID.fullmatch(run.name)
    number = artifact.pull_number or (name["pr_number"] if name else None)
    entry = run / "review-entry.json"
    if number != _target_pr_number(target) or not (entry.exists() or entry.is_symlink()):
        return None
    try:
        record = _entry_object(entry)
    except (ValueError, OSError, LookupError):
        # A damaged allocation with the right number still needs to block recovery if it is the latest.
        return True
    if "pr_url" not in record:
        return None
    url = record["pr_url"]
    match = PR_URL_PATTERN.fullmatch(url) if isinstance(url, str) else None
    return not match or match["number"] != number or _matches_target(target, number, url)


def _producer_allocation(artifact: ReviewArtifact) -> str:
    """Read the UTC allocation time from a retained entry or its original staging directory."""
    run = artifact.path.parent
    entry = run / "review-entry.json"
    if entry.exists() or entry.is_symlink():
        record = _entry_object(entry)
        name = record.get("allocated_as", "")
        match = TIMESTAMP_RUN_ID.fullmatch(name) if isinstance(name, str) else None
        if (
            record.get("schema_version") != 1
            or not match
            or not match["pr_number"]
            or (artifact.pull_number is not None and match["pr_number"] != artifact.pull_number)
            or (artifact.pull_number is None and run.name != name)
        ):
            raise LookupError(f"invalid-review-entry:{run}")
    elif artifact.pull_number is None:
        name = run.name
    else:
        # A sequence orders historical siblings, but has no shared clock with other producers.
        raise LookupError(f"ambiguous-review-allocation-time:{run}")

    # Older flat runs used whole UTC seconds. Normalize both actual allocation formats to microseconds;
    # arbitrary directory names, filesystem timestamps and impossible dates cannot establish chronology.
    if TIMESTAMP_RUN_ID.fullmatch(name):
        stamp = name.split("Z", 1)[0]
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z", name):
        stamp = name[:-1] + "-000000"
    else:
        raise LookupError(f"ambiguous-review-allocation-time:{run}")
    try:
        datetime.strptime(stamp, "%Y-%m-%dT%H-%M-%S-%f")
    except ValueError as error:
        raise LookupError(f"ambiguous-review-allocation-time:{run}") from error
    return stamp


def _latest_producer(matches: list[ReviewArtifact]) -> ReviewArtifact:
    """Keep root and numbered sequence precedence while considering newer current-format staging allocations."""
    latest = max(matches, key=lambda artifact: artifact.order)
    if latest.pull_number is None:
        return latest
    staging = [
        artifact
        for artifact in matches
        if artifact.order[0] == latest.order[0]
        and artifact.pull_number is None
        and TIMESTAMP_RUN_ID.fullmatch(artifact.path.parent.name)
    ]
    if staging:
        pending = max(staging, key=lambda artifact: artifact.order)
        if _producer_allocation(pending) > _producer_allocation(latest):
            return pending
    return latest


def _latest_across_roots(artifacts: list[ReviewArtifact], *, producer: bool = False) -> ReviewArtifact:
    """Keep each root's own sequence, then compare independent producers by their retained allocation time."""
    groups: dict[Path, list[ReviewArtifact]] = {}
    priority = max(artifact.order[0] for artifact in artifacts)
    for artifact in artifacts:
        if artifact.order[0] == priority:
            run = artifact.path.parent
            root = run.parent.parent if artifact.pull_number else run.parent
            groups.setdefault(root, []).append(artifact)
    finalists = [
        _latest_producer(group) if producer else max(group, key=lambda artifact: artifact.order)
        for group in groups.values()
    ]
    if len(finalists) == 1:
        return finalists[0]
    ranked = sorted(((_producer_allocation(artifact), artifact) for artifact in finalists), reverse=True)
    if ranked[0][0] == ranked[1][0]:
        raise LookupError("ambiguous-review-allocation-time")
    return ranked[0][1]


def _frozen_local_match(run: Path, identity: dict, *, require_plan: bool) -> bool:
    """Recover a historical local working-tree or path selection from its frozen collector and preparation evidence."""
    directory = run / "local-source"
    receipt_file = directory / "review-worktree.json"
    if identity["scope"] == "commit" or directory.is_symlink() or not receipt_file.is_file():
        return False
    receipt = _entry_object(receipt_file)
    if receipt.get("source_worktree") != identity["repository"] or receipt.get("revision") != identity["revision"]:
        return False
    target = identity["target"] if identity["scope"] == "path" else None
    if require_plan:
        if (run / "direct-plan.json").is_file():
            arguments = _entry_object(run / "direct-plan.json")
        elif (run / "batch-inventory.json").is_file():
            arguments = _entry_object(run / "batch-inventory.json").get("source_arguments")
        else:
            return False
        if not isinstance(arguments, dict) or arguments.get("source_root") != receipt.get("review_worktree"):
            return False
        if "scope_path" not in arguments or arguments["scope_path"] != target:
            return False
    snapshot = _entry_object(directory / "source-snapshot.json")
    if snapshot.get("repository") != identity["repository"] or snapshot.get("revision") != identity["revision"]:
        return False
    records = snapshot["files"]
    staged = directory / "staged.patch"
    if staged.is_symlink() or not staged.is_file():
        return False
    staged_bytes = staged.read_bytes()
    if target and target != ".":
        records = [r for r in records if r["path"] == target or r["path"].startswith(target + "/")]
        changes = diff_sections(staged_bytes)
        staged_bytes = b"".join(
            section for path, section in changes.items() if path == target or path.startswith(target + "/")
        )
    patch = run / "diff.patch"
    return not patch.is_symlink() and (
        hashlib.sha256(patch.read_bytes()).hexdigest() == identity["diff_sha256"]
        and review_file_digest(records) == identity["files_sha256"]
        and hashlib.sha256(staged_bytes).hexdigest() == identity["staged_sha256"]
    )


def latest_local_run(
    root: Path, identity: dict, *, codex_home: Path | None = None, parent_thread_id: str | None = None
) -> Path:
    """Select the newest proven local entry, rejecting redirected, terminal and damaged evidence for that run."""
    # The allocator's UTC names order only those directories whose independent selection evidence matches.
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise LookupError("redirected-review-run")
    for run in sorted(root.glob("*"), reverse=True):
        if not TIMESTAMP_RUN_ID.fullmatch(run.name) or not run.is_dir():
            continue
        if run.is_symlink():
            raise LookupError(f"redirected-review-run:{run}")
        entry = run / "review-entry.json"
        if entry.exists() or entry.is_symlink():
            record = _entry_object(entry)
            if record.get("local") != identity:
                continue
            if record.get("schema_version") != 1 or record.get("allocated_as") != run.name:
                raise LookupError(f"invalid-review-entry:{run}")
            if (run / "local-source" / "review-worktree.json").exists() and not _frozen_local_match(
                run, identity, require_plan=False
            ):
                raise LookupError(f"local-review-frozen-source-mismatch:{run}")
        elif not _frozen_local_match(run, identity, require_plan=True):
            continue
        for path in (run / "result.json", run / CANDIDATE_RESULT_NAME, run / "diff.patch"):
            if path.is_symlink():
                raise LookupError(f"redirected-review-run:{path}")
        patch = run / "diff.patch"
        if (
            patch.is_file()
            and "diff_sha256" in identity
            and hashlib.sha256(patch.read_bytes()).hexdigest() != identity["diff_sha256"]
        ):
            raise LookupError(f"local-review-collected-diff-mismatch:{run}")
        if (run / "result.json").is_file():
            metadata = _entry_object(run / "result.json").get("metadata")
            if not isinstance(metadata, dict) or metadata.get("scope") != identity["scope"]:
                raise LookupError(f"invalid-review-report-rerun-code-review:{run}")
            if review_result_kind(run / "result.json", allow_local=True) == "assessed":
                revision = identity["revisions"][0] if identity["scope"] == "commit" else identity["revision"]
                if metadata.get("reviewed_commit_sha") != revision:
                    raise LookupError(f"local-review-result-revision-mismatch:{run}")
            require_assessed_review_result(
                run / "result.json", codex_home=codex_home, parent_thread_id=parent_thread_id
            )
        return run
    raise LookupError("missing-matching-review-report")


def find_latest_review_report(target: str, reports_dirs: list[Path], *, include_pending: bool = False) -> Path:
    """Discover the newest result; callers must validate it before treating it as assessed evidence."""
    normalized_target = target.strip().rstrip("/")
    matches: list[ReviewArtifact] = []
    candidate_matches: list[ReviewArtifact] = []
    promoted_matches: list[ReviewArtifact] = []
    unavailable_matches: list[ReviewArtifact] = []
    invalid_matches: list[ReviewArtifact] = []
    closed_matches: list[ReviewArtifact] = []
    incomplete_matches: list[ReviewArtifact] = []

    for reports_dir in reports_dirs:
        # Run numbers are allocated within each root; the current namespace supersedes legacy runs.
        root_priority = _root_priority(reports_dir)
        artifacts = _review_artifacts(reports_dir, root_priority=root_priority)
        for artifact in artifacts:
            result_path = artifact.path
            # A verified allocation identifies an interrupted or conflicting collection for producer recovery.
            allocated_match = _allocated_target_matches(artifact, normalized_target) if include_pending else None
            kind = review_result_kind(result_path) if result_path.name == "result.json" else None
            if not allocated_match and not _artifact_matches_target(
                artifact,
                normalized_target,
                allow_target_file=True,
                allow_unverified_directory=allocated_match is None and kind != "unavailable",
            ):
                continue
            if result_path.name == CANDIDATE_RESULT_NAME:
                candidate_matches.append(artifact)
                continue
            if kind is None:
                incomplete_matches.append(artifact)
                continue
            if kind == "unavailable":
                # Collection failures contain no assessment and cannot clear an intervening incomplete review.
                unavailable_matches.append(artifact)
                continue
            # Damaged identity blocks admission, but must not let an older run choose the recovery anchor.
            promoted_matches.append(artifact)
            identity = _read_pr_identity(result_path.parent / "pr.json")
            if (
                identity is None
                or not identity[2]
                or (artifact.pull_number is not None and artifact.pull_number != identity[0])
            ):
                invalid_matches.append(artifact)
                continue
            if kind == "closed":
                closed_matches.append(artifact)
                continue
            if kind != "assessed":
                invalid_matches.append(artifact)
                continue
            matches.append(artifact)

    if include_pending:
        # The producer may resume a pending run. Remediation still admits only validated results.
        all_matches = incomplete_matches + candidate_matches + unavailable_matches + promoted_matches
        if all_matches:
            latest = _latest_across_roots(all_matches, producer=True)
            run = latest.path.parent
            name = TIMESTAMP_RUN_ID.fullmatch(run.name)
            metadata = run / "pr.json"
            requested = run / "pr-target.txt"
            entry = run / "review-entry.json"
            receipt = _entry_object(entry) if entry.exists() or entry.is_symlink() else {}
            staging = (
                latest in incomplete_matches and name and name["pr_number"] == _target_pr_number(normalized_target)
            )
            if "pr_url" in receipt:
                _producer_allocation(latest)  # Validate the allocation name even without an older numbered run.
                url = receipt["pr_url"]
                if (
                    not same_pr_url(normalized_target, url)
                    or requested.is_symlink()
                    or (requested.exists() and not same_pr_url(url, requested.read_text(encoding="utf-8").strip()))
                ):
                    raise LookupError(f"unverified-review-run-identity:{run}")
            # Only a staging producer may use its preflight or requested URL before authoritative collection.
            pending_identity = (
                staging
                and not metadata.exists()
                and not metadata.is_symlink()
                and not requested.is_symlink()
                and ("pr_url" in receipt or _artifact_matches_target(latest, normalized_target, allow_target_file=True))
            )
            if not pending_identity and not _artifact_matches_target(latest, normalized_target):
                raise LookupError(f"unverified-review-run-identity:{run}")
            if latest in incomplete_matches or latest in candidate_matches:
                return latest.path
            if latest in closed_matches:
                raise LookupError(f"matching-review-closed-not-remediable:{latest.path}")
            if latest in invalid_matches:
                raise LookupError(f"invalid-review-report-rerun-code-review:{latest.path}")
            if latest in matches:
                return latest.path
            if latest in unavailable_matches:
                raise LookupError(f"matching-review-unavailable-rerun-code-review:{latest.path}")
    # A candidate beside a promoted result still blocks intake. An unavailable collection cannot hide an assessment.
    retained = candidate_matches + promoted_matches + incomplete_matches
    if not retained:
        if unavailable_matches:
            unavailable = _latest_across_roots(unavailable_matches)
            raise LookupError(f"matching-review-unavailable-rerun-code-review:{unavailable.path}")
        raise LookupError("missing-matching-review-report")
    latest = _latest_across_roots(retained)
    if latest in candidate_matches:
        raise LookupError(f"matching-review-candidate-unpromoted:{latest.path}")
    if latest in incomplete_matches:
        raise LookupError(f"matching-review-incomplete:{latest.path.parent}")
    if latest in invalid_matches:
        raise LookupError(f"invalid-review-report-rerun-code-review:{latest.path}")
    if latest in closed_matches:
        raise LookupError(f"matching-review-closed-not-remediable:{latest.path}")
    return latest.path


def validate_review_result(
    result_path: Path, *, codex_home: Path | None = None, parent_thread_id: str | None = None
) -> dict[str, Any]:
    """Run both artifact validators on the selected bytes, retaining supported historical schemas."""
    result_path = result_path.resolve()
    run_dir = result_path.parent
    plugin_root = Path(__file__).resolve().parents[1]
    review_command = [
        sys.executable,
        str(plugin_root / "skills/code-review/validate_artifacts.py"),
        "--out",
        str(run_dir),
        "--result",
        str(result_path),
    ]
    if codex_home is not None:
        review_command.extend(["--codex-home", str(codex_home)])
    if parent_thread_id is not None:
        review_command.extend(["--parent-thread-id", parent_thread_id])
    shared_command = [
        sys.executable,
        str(plugin_root / "shared/validate-artifacts.py"),
        "--skill",
        "code-review",
        "--out",
        str(run_dir),
        "--result",
        str(result_path),
    ]
    result_bytes = result_path.read_bytes()
    for command in (review_command, shared_command):
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)  # noqa: S603 - argv list, no shell
        if completed.returncode:
            diagnostic = (completed.stderr or completed.stdout).strip()
            raise LookupError(f"review-validation-failed:{result_path}:{diagnostic}")
    payload = json.loads(result_bytes)
    if result_path.read_bytes() != result_bytes:
        raise LookupError("review-completion-result-changed")
    return payload


def complete_review_run(run_dir: Path, *, codex_home: Path | None = None, parent_thread_id: str | None = None) -> bytes:
    """Emit the explicitly named validated review without claiming it is the newest intake candidate.

    This read-only boundary never repairs or promotes artifacts. Intake shares its validators, but only new producer
    completion requires bound schema-v3 final bytes for every disposition. Historical reports remain readable as
    unverified archives, not completion or remediation evidence. Without an explicit override, retained manifest
    identity selects the producer thread; validators still authenticate that producer's execution and source.
    """
    run_dir = run_dir.resolve()
    result_path = run_dir / "result.json"
    if not result_path.is_file():
        raise LookupError(f"review-result-not-promoted:{run_dir}")
    result_bytes = result_path.read_bytes()
    manifest_path = run_dir / "specialist-manifest.json"
    if parent_thread_id is None and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        producer = manifest.get("parent_thread_id") if isinstance(manifest, dict) else None
        if not isinstance(producer, str) or not producer.strip():
            raise LookupError("review-validation-failed:missing-producer-thread")
        parent_thread_id = producer
    payload = validate_review_result(result_path, codex_home=codex_home, parent_thread_id=parent_thread_id)
    if payload.get("schema_version") != 3:
        raise LookupError("review-completion-requires-bound-schema-v3")
    metadata = payload["metadata"]
    if metadata.get("review_status") not in {"unavailable", "closed"} and (
        metadata.get("finding_records_version") != 2 or metadata.get("review_validation", {}).get("schema_version") != 1
    ):
        raise LookupError(
            "review-source-contract-upgrade-required:rerun-code-review-or-use --archive-result for read-only access"
        )
    if result_path.read_bytes() != result_bytes:
        raise LookupError("review-completion-result-changed")
    final_bytes = (run_dir / "final.md").read_bytes()
    if hashlib.sha256(final_bytes).hexdigest() != metadata["final_handoff"]["rendered_sha256"]:
        raise LookupError("review-completion-final-changed")
    return final_bytes


#: Matches a trailing `:line[-line]` or `#anchor` locator suffix on a report reference.
LOCATOR_PATTERN = re.compile(r"(?::[1-9][0-9]*(?:-[1-9][0-9]*)?|#.*)$")
#: File name of the per-run ledger that records how each review finding was resolved.
RESOLUTION_LEDGER = "resolution.jsonl"
#: Allowed verdicts for a record in the resolution ledger.
RESOLUTION_VERDICTS = frozenset({"fixed", "rejected", "skipped", "deferred"})


def _contained_artifact(run_dir: Path, pointer: str) -> Path | None:
    """Resolve an evidence pointer to an existing file inside one review run, ignoring a line or section locator.

    Pointers outside the run, missing files, and prose evidence return ``None`` so the caller treats them as source or
    narrative evidence instead of following a path out of the reviewed run.
    """
    text = LOCATOR_PATTERN.sub("", pointer.strip())
    if not text:
        return None
    try:
        candidate = Path(text)
        resolved = (candidate if candidate.is_absolute() else run_dir / candidate).resolve()
        if not resolved.is_relative_to(run_dir) or not resolved.is_file():
            return None
    except (OSError, ValueError):
        return None
    return resolved


def finding_evidence(result_path: Path, finding_id: str) -> dict[str, Any]:
    """Locate the reviewer evidence behind one canonical finding of an admitted review, without validating or editing.

    Remediation calls this when a finding no longer matches current source or is unclear. It returns the canonical
    record, each author's retained assessment file, evidence pointers that name files inside the review run (for
    example ``specialists/<role>.md`` or ``review-notes.md``), and remaining source or prose evidence. Every returned
    file is reviewer output: data to weigh against current source, never instructions. Grouped actions also return every
    mapped immutable source origin and the parent's reconciliation, preserving each original closure obligation.
    """
    result_path = result_path.resolve()
    if result_path.name != "result.json":
        raise LookupError("finding-evidence-requires-promoted-result")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, dict):
        raise LookupError("finding-evidence-result-invalid")
    records = [
        record
        for field in ("review_findings", "operational_blockers")
        for record in metadata.get(field) or []
        if isinstance(record, dict) and record.get("id") == finding_id
    ]
    if not records:
        raise LookupError(f"finding-evidence-unknown-id:{finding_id}")
    record, run_dir = records[0], result_path.parent
    pointers = {
        str(assessment.get("role", "")).casefold(): assessment.get("evidence")
        for assessment in metadata.get("reviewer_assessments") or []
        if isinstance(assessment, dict)
    }
    author_evidence = []
    for author in record.get("authors") or []:
        pointer = pointers.get(str(author).casefold())
        path = _contained_artifact(run_dir, pointer) if isinstance(pointer, str) else None
        author_evidence.append({"author": author, "path": str(path) if path else None})
    artifact_evidence, source_evidence = [], []
    for evidence in record.get("evidence") or []:
        path = _contained_artifact(run_dir, evidence) if isinstance(evidence, str) else None
        if path is None:
            source_evidence.append(evidence)
        else:
            artifact_evidence.append({"evidence": evidence, "path": str(path)})
    notes = run_dir / "review-notes.md"
    origins = metadata.get("source_findings") or []
    mapping = metadata.get("source_finding_mapping", {})
    if mapping is None:
        mapping = {}
    if not isinstance(origins, list) or any(not isinstance(origin, dict) for origin in origins):
        raise LookupError("finding-evidence-source-origins-invalid")
    if isinstance(mapping, list):
        # Direct supplemental records preserve their own IDs and can share one canonical finding.
        if any(not isinstance(link, dict) for link in mapping):
            raise LookupError("finding-evidence-source-mapping-invalid")
        origin_ids = [origin.get("id") for origin in origins if origin.get("candidate_id") == finding_id]
        linked_ids = [link.get("source_id") for link in mapping if link.get("candidate_id") == finding_id]
        if (
            any(not isinstance(value, str) or not value for value in origin_ids + linked_ids)
            or len(set(origin_ids)) != len(origin_ids)
            or len(set(linked_ids)) != len(linked_ids)
            or set(origin_ids) != set(linked_ids)
            or any(link.get("source_id") in origin_ids and link.get("candidate_id") != finding_id for link in mapping)
            or any(origin.get("id") in origin_ids and origin.get("candidate_id") != finding_id for origin in origins)
        ):
            raise LookupError("finding-evidence-source-mapping-invalid")
        source_origins = [origin for origin in origins if origin.get("id") in origin_ids]
    elif isinstance(mapping, dict):
        if any(origin.get("candidate_id") == finding_id for origin in origins):
            raise LookupError("finding-evidence-source-mapping-invalid")
        source_origins = [origin for origin in origins if mapping.get(origin.get("finding_id")) == [finding_id]]
    else:
        raise LookupError("finding-evidence-source-mapping-invalid")
    return {
        "review_run": str(run_dir),
        "finding": record,
        "author_evidence": author_evidence,
        "artifact_evidence": artifact_evidence,
        "source_evidence": source_evidence,
        "review_notes": str(notes) if notes.is_file() else None,
        "source_origins": source_origins,
        "source_reconciliation": (metadata.get("source_finding_reconciliation") or {}).get(finding_id),
    }


def _finding_titles(run_dir: Path) -> dict[str, str]:
    """Read canonical finding titles from a prior run's result, tolerating an unreadable or historical result."""
    try:
        metadata = json.loads((run_dir / "result.json").read_text(encoding="utf-8")).get("metadata") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}
    return {
        record["id"]: record.get("title", "")
        for field in ("review_findings", "operational_blockers")
        for record in metadata.get(field) or []
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }


def prior_resolutions(run_dir: Path) -> dict[str, Any]:
    """Summarize how remediation resolved the newest earlier review of the same pull request.

    The current run must be promoted. Select prior siblings by sequence, and prior runs from other managed roots by PR
    identity and allocation time. Only a promoted result with a resolution ledger supplies feedback. Later lines
    supersede earlier ones for the same finding. Malformed lines are counted. A first review has no prior resolutions.
    """
    run_dir = run_dir.resolve()
    run_match = RUN_DIRECTORY_PATTERN.fullmatch(run_dir.name)
    if run_match is None or PR_DIRECTORY_PATTERN.fullmatch(run_dir.parent.name) is None:
        raise LookupError("prior-resolutions-requires-pr-run")
    current = int(run_match.group(1))
    root = run_dir.parent.parent
    identity = _read_pr_identity(run_dir / "pr.json")
    url = identity[1] if identity and identity[2] else ""
    roots = _pr_roots_for_directory(url, root)[1] if url else [root]
    current_artifact = ReviewArtifact(
        run_dir / "result.json", (_root_priority(root), 1, current), run_dir.parent.name[3:]
    )
    earlier: list[ReviewArtifact] = []
    for directory in roots:
        priority = _root_priority(directory)
        if priority > current_artifact.order[0]:
            continue
        for sibling in (directory / run_dir.parent.name).glob("run-*"):
            match = RUN_DIRECTORY_PATTERN.fullmatch(sibling.name)
            if not match or int(match[1]) == 0:
                continue
            result = sibling / "result.json"
            if not (sibling / RESOLUTION_LEDGER).is_file() or not result.is_file():
                continue
            artifact = ReviewArtifact(result, (priority, 1, int(match[1])), current_artifact.pull_number)
            if directory == root:
                if int(match[1]) < current:
                    earlier.append(artifact)
                continue
            # Run numbers are unrelated across repositories. Require an independent PR identity before sharing feedback.
            if not _artifact_matches_target(artifact, url):
                continue
            if any(
                path.is_symlink()
                for path in (sibling.parent, sibling, result, sibling / RESOLUTION_LEDGER, sibling / "pr.json")
            ):
                raise LookupError("redirected-review-run")
            if priority < current_artifact.order[0] or _producer_allocation(artifact) < _producer_allocation(
                current_artifact
            ):
                earlier.append(artifact)
    if not earlier:
        return {"review_run": None, "ledger": None, "resolutions": [], "invalid_lines": 0}
    prior = _latest_across_roots(earlier).path.parent
    ledger = prior / RESOLUTION_LEDGER
    latest: dict[str, dict[str, Any]] = {}
    invalid = 0
    with ledger.open("rb") as stream:
        content = stream.read(MAX_LEDGER_BYTES + 1)
    if len(content) > MAX_LEDGER_BYTES:
        raise ValueError("prior-resolutions-ledger-oversized")
    for line in content.decode("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("finding_id"), str)
            or record.get("verdict") not in RESOLUTION_VERDICTS
        ):
            invalid += 1
            continue
        latest[record["finding_id"]] = record
    titles = _finding_titles(prior)
    resolutions = [
        {
            "finding_id": finding_id,
            "title": titles.get(finding_id, ""),
            "verdict": record["verdict"],
            "sha": record.get("sha"),
            "why": record.get("why", ""),
        }
        for finding_id, record in latest.items()
    ]
    return {"review_run": str(prior), "ledger": str(ledger), "resolutions": resolutions, "invalid_lines": invalid}


def main(argv: list[str] | None = None) -> int:
    """Select a report or run the producer's fail-closed completion gate."""
    parser = argparse.ArgumentParser(description="Print the newest Codex code-review result matching a PR target.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--target", help="PR number, #number, or PR URL.")
    source.add_argument(
        "--local-scope", choices=("working-tree", "path", "commit"), help="Standalone local review scope."
    )
    parser.add_argument("--local-target", default="", help="Selected local path or Git revision expression.")
    source.add_argument("--result", type=Path, help="Explicit result path that must be an assessed review.")
    source.add_argument(
        "--archive-result", type=Path, help="Describe a schema-2 or schema-3 result as unverified archive data."
    )
    source.add_argument("--complete-run", type=Path, help="Validate a promoted run and emit only its bound final text.")
    source.add_argument(
        "--finding-evidence", type=Path, help="Admitted review result.json whose finding evidence to locate."
    )
    source.add_argument(
        "--prior-resolutions", type=Path, help="Current pr-<n>/run-<NNN> directory; print the prior run's resolutions."
    )
    parser.add_argument(
        "--latest-run", action="store_true", help="Print the latest matching standalone review run directory."
    )
    parser.add_argument(
        "--expect-run", type=Path, help="Confirm that the offered --latest-run directory is still current."
    )
    parser.add_argument("--finding-id", help="Canonical finding or blocker ID for --finding-evidence.")
    parser.add_argument("--codex-home", type=Path, help="Completion or intake validator's rollout-log root.")
    parser.add_argument("--parent-thread-id", help="Completion or intake validator's producer thread identity.")
    parser.add_argument(
        "--reports-dir",
        type=Path,
        help="Directory containing PR-scoped runs or legacy timestamped code-review reports.",
    )
    args = parser.parse_args(argv)

    if args.expect_run is not None and not args.latest_run:
        parser.error("--expect-run requires --latest-run")
    if args.local_scope and not args.latest_run:
        parser.error("--local-scope requires --latest-run")
    if bool(args.local_target) != (args.local_scope in {"path", "commit"}):
        parser.error("--local-target is required only for path or commit review")
    if (
        args.latest_run
        and not args.local_scope
        and not PR_URL_PATTERN.fullmatch((args.target or "").strip().rstrip("/"))
    ):
        parser.error("--latest-run requires --target with a canonical PR URL")
    if args.finding_evidence is not None and not args.finding_id:
        parser.error("--finding-evidence requires --finding-id")
    try:
        if args.finding_evidence is not None:
            print(json.dumps(finding_evidence(args.finding_evidence, args.finding_id), indent=2, sort_keys=True))
            return 0
        if args.prior_resolutions is not None:
            print(json.dumps(prior_resolutions(args.prior_resolutions), indent=2, sort_keys=True))
            return 0
        if args.archive_result is not None:
            print(json.dumps(read_archive_result(args.archive_result), sort_keys=True))
            return 0
        if args.complete_run is not None:
            final_bytes = complete_review_run(
                args.complete_run, codex_home=args.codex_home, parent_thread_id=args.parent_thread_id
            )
            sys.stdout.buffer.write(final_bytes)
            return 0
        if args.result is not None:
            print(
                require_assessed_review_result(
                    args.result,
                    codex_home=args.codex_home,
                    parent_thread_id=args.parent_thread_id,
                    reports_dir=args.reports_dir,
                )
            )
            return 0
        if args.local_scope:
            reports_dir = args.reports_dir or CURRENT_REPORTS_DIR
            if not reports_dir.is_dir():
                raise LookupError("missing-matching-review-report")
            local = local_review_identity(Path.cwd(), args.local_scope, args.local_target)
            run = latest_local_run(
                reports_dir, local, codex_home=args.codex_home, parent_thread_id=args.parent_thread_id
            )
            if args.expect_run is not None and run.resolve() != args.expect_run.resolve():
                raise LookupError("selected-review-run-changed")
            print(run.resolve())
            return 0
        target, reports_dirs = (
            _pr_roots_for_directory(args.target, args.reports_dir.absolute())
            if args.reports_dir is not None
            else _default_pr_roots(args.target)
        )
        selected = find_latest_review_report(target, reports_dirs, include_pending=args.latest_run)
        if args.latest_run:
            # The next workflow step can write here. Reject redirected runs and retained identity files.
            root = next((directory for directory in reports_dirs if selected.is_relative_to(directory)), None)
            if (
                root is None
                or any(path.is_symlink() for path in (selected, *selected.parents) if path.is_relative_to(root))
                or (selected.parent / "pr.json").is_symlink()
            ):
                raise LookupError("redirected-review-run")
        if selected.name == "result.json" and args.latest_run:
            complete_review_run(selected.parent, codex_home=args.codex_home, parent_thread_id=args.parent_thread_id)
        elif selected.name == "result.json":
            require_assessed_review_result(selected, codex_home=args.codex_home, parent_thread_id=args.parent_thread_id)
        if args.expect_run is not None and selected.parent.resolve() != args.expect_run.resolve():
            raise LookupError("selected-review-run-changed")
        print(selected.parent.resolve() if args.latest_run else selected)
    except (LookupError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        if args.complete_run is not None:
            print(
                f"Review handoff blocked: {exc}\nRetained evidence: {args.complete_run}. Review not complete.",
                file=sys.stderr,
            )
        else:
            print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
