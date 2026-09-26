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
artifact path. Matching accepts a PR number, ``#number``, or exact normalized URL and checks ``pr.json`` identity and
result metadata.

## Usage

Run ``python find-review-report.py --target <pr-url-or-number>`` to select an assessed report. Alternatively, run the
same script with ``--result <path>`` to accept assessed working-tree, path, commit, or PR reports while rejecting
supplied unavailable, closed, or unpromoted candidate results. The target search covers canonical
``pr-<number>/run-<NNN>`` directories, timestamped flat reports, and, for the default current root, the legacy
``.reports/codex/review`` root. ``--complete-run <run>`` validates a promoted review, checks that PR lookup returns that
exact result, then emits the bound final Markdown bytes. It never promotes candidates.

## Used by

The ``code-remediate #<PR> +review`` workflow and review-report lookup tests use this selector. Remediation is the
consumer of the assessed/unavailable distinction because it must rerun code-review when no trustworthy prior assessment
exists.

## Outputs

It prints one matching assessed local review-artifact path. The current report root supersedes the legacy root for the
same PR because run numbers are allocated independently in each root; within one root, the numerically highest
PR-scoped run supersedes flat timestamped artifacts. Target lookup requires PR scope and a
recognized ``metadata.review_decision.recommendation`` in addition to PR identity. Explicit intake accepts all four
declared review scopes without requiring PR identity; current intake artifacts must be canonical schema-3
``result.json`` files that pass both validators. Existing ``--parent-thread-id`` and ``--codex-home`` options identify
evidence from another producer session. PR intake defaults to the recorded producer thread, not the consuming session,
while revalidating its evidence.
``--archive-result <path>`` reports a schema-2 or schema-3 result as unverified data and never returns a
remediation-eligible path.
Local completion retains current runtime defaults. It does not broaden target lookup.

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
from pathlib import Path
from typing import Any, NamedTuple


def _read_pr_identity(pr_path: Path) -> tuple[str, str] | None:
    """Read a PR identity without mistaking malformed collector output for a match."""
    try:
        payload: dict[str, Any] = json.loads(pr_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    raw_number = payload.get("number")
    if type(raw_number) is not int or raw_number <= 0:
        return None
    raw_url = payload.get("url", "")
    if not isinstance(raw_url, str):
        return None
    number = str(raw_number)
    url = raw_url.rstrip("/")
    return number, url


def _matches_target(target: str, number: str, url: str) -> bool:
    """Return whether a canonical PR identity matches a user target string."""
    return bool(target and (number == target.lstrip("#") or url == target))


def review_result_kind(result_path: Path, *, allow_local: bool = False) -> str:
    """Classify review disposition, accepting local scopes only for explicit-path intake."""
    try:
        payload: dict[str, Any] = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
    if not isinstance(decision, dict) or decision.get("recommendation") not in {
        "accept-as-is",
        "minor-changes",
        "needs-more-work",
        "reject",
        "not-aligned",
    }:
        return "invalid"
    return "assessed"


def require_assessed_review_result(
    result_path: Path, *, codex_home: Path | None = None, parent_thread_id: str | None = None
) -> Path:
    """Validate assessed artifacts before explicit or discovered report intake.

    Local intake reuses the producer completion boundary and its optional evidence-location and thread overrides. It
    returns only the selected path, never the producer's final text.
    """
    if result_path.name == CANDIDATE_RESULT_NAME:
        raise LookupError(f"matching-review-candidate-unpromoted:{result_path}")
    kind = review_result_kind(result_path, allow_local=True)
    if kind == "unavailable":
        raise LookupError("matching-review-unavailable-rerun-code-review")
    if kind == "closed":
        raise LookupError("matching-review-closed-not-remediable")
    if kind != "assessed":
        raise LookupError("invalid-review-report-rerun-code-review")
    if result_path.name != "result.json":
        raise LookupError("invalid-review-report-rerun-code-review")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 3:
        raise LookupError("historical-review-unverified-archive:use --archive-result for read-only access")
    if payload["metadata"]["scope"] == "pr":
        # Intake can run in a later session. Revalidate the recorded producer's evidence, not the consumer's identity.
        manifest_path = result_path.parent / "specialist-manifest.json"
        if parent_thread_id is None and manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            producer = manifest.get("parent_thread_id") if isinstance(manifest, dict) else None
            if not isinstance(producer, str) or not producer.strip():
                raise LookupError("review-validation-failed:missing-producer-thread")
            parent_thread_id = producer
        complete_review_run(result_path.parent, codex_home=codex_home, parent_thread_id=parent_thread_id)
    else:
        complete_review_run(result_path.parent, codex_home=codex_home, parent_thread_id=parent_thread_id)
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


CURRENT_REPORTS_DIR = Path(".reports/codex/code-review")
LEGACY_REPORTS_DIR = Path(".reports/codex/review")
CANDIDATE_RESULT_NAME = "result.candidate.json"
PR_DIRECTORY_PATTERN = re.compile(r"pr-([1-9][0-9]*)")
RUN_DIRECTORY_PATTERN = re.compile(r"run-([0-9]{3,})")
PR_URL_PATTERN = re.compile(r"https://github[.]com/[^/]+/[^/]+/pull/([1-9][0-9]*)")


class ReviewArtifact(NamedTuple):
    """Bind a review result path to its topology-aware ordering identity."""

    path: Path
    order: tuple[int, int, str | int]
    pull_number: str | None


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
                # Collection records the requested PR before notes, identity, or a terminal result.
                for pending_name in ("pr-target.txt", "pr.json"):
                    pending = report_dir / pending_name
                    if pending.is_file():
                        artifacts.append(ReviewArtifact(pending, order, None))
                        break
    return artifacts


def _target_pr_number(target: str) -> str | None:
    """Extract the PR number from an advertised numeric or canonical GitHub target."""
    numeric = target.lstrip("#")
    if numeric.isdecimal() and int(numeric) > 0:
        return str(int(numeric))
    match = PR_URL_PATTERN.fullmatch(target)
    return match.group(1) if match else None


def _artifact_matches_target(
    artifact: ReviewArtifact, target: str, *, allow_target_file: bool = False, allow_unverified_directory: bool = False
) -> bool:
    """Match verified PR identity or conservatively identify a pending run."""
    identity = _read_pr_identity(artifact.path.parent / "pr.json")
    if identity is not None:
        number, url = identity
        if artifact.pull_number is not None and artifact.pull_number != number:
            return allow_unverified_directory and artifact.pull_number == _target_pr_number(target)
        if _matches_target(target, number, url):
            return True
        return allow_unverified_directory and not url and artifact.pull_number == _target_pr_number(target)
    if allow_unverified_directory and artifact.pull_number == _target_pr_number(target):
        return True
    if not allow_target_file:
        return False
    target_path = artifact.path.parent / "pr-target.txt"
    stored_target = target_path.read_text(encoding="utf-8").strip() if target_path.is_file() else ""
    if _matches_target(target, stored_target, stored_target):
        return True
    return target.lstrip("#").isdecimal() and _target_pr_number(target) == _target_pr_number(stored_target)


def find_latest_review_report(target: str, reports_dirs: list[Path]) -> Path:
    """Discover the newest result; callers must validate it before treating it as assessed evidence."""
    normalized_target = target.strip().rstrip("/")
    matches: list[ReviewArtifact] = []
    candidate_matches: list[ReviewArtifact] = []
    promoted_matches: list[ReviewArtifact] = []
    unavailable_matches = False
    invalid_matches: list[ReviewArtifact] = []
    closed_matches: list[ReviewArtifact] = []
    incomplete_matches: list[ReviewArtifact] = []

    for reports_dir in reports_dirs:
        # Run numbers are allocated within each root; the current namespace supersedes legacy runs.
        root_priority = int(reports_dir.resolve() == CURRENT_REPORTS_DIR.resolve())
        artifacts = _review_artifacts(reports_dir, root_priority=root_priority)
        for artifact in artifacts:
            result_path = artifact.path
            if result_path.name in {"review-notes.md", "pr.json", "pr-target.txt"}:
                if _artifact_matches_target(
                    artifact, normalized_target, allow_target_file=True, allow_unverified_directory=True
                ):
                    incomplete_matches.append(artifact)
                continue
            if result_path.name == CANDIDATE_RESULT_NAME:
                if _artifact_matches_target(
                    artifact, normalized_target, allow_target_file=True, allow_unverified_directory=True
                ):
                    candidate_matches.append(artifact)
                continue
            kind = review_result_kind(result_path)
            if kind == "unavailable":
                if _artifact_matches_target(artifact, normalized_target, allow_target_file=True):
                    unavailable_matches = True
                # Collection failures contain no assessment and cannot clear an intervening incomplete review.
                continue
            if not _artifact_matches_target(
                artifact, normalized_target, allow_target_file=True, allow_unverified_directory=True
            ):
                continue
            identity = _read_pr_identity(result_path.parent / "pr.json")
            if identity is None or (artifact.pull_number is not None and artifact.pull_number != identity[0]):
                invalid_matches.append(artifact)
                continue
            promoted_matches.append(artifact)
            if kind == "closed":
                closed_matches.append(artifact)
                continue
            if kind != "assessed":
                invalid_matches.append(artifact)
                continue
            matches.append(artifact)

    matches.sort(key=lambda artifact: artifact.order, reverse=True)
    closed_matches.sort(key=lambda artifact: artifact.order, reverse=True)
    candidate_matches.sort(key=lambda artifact: artifact.order, reverse=True)
    promoted_matches.sort(key=lambda artifact: artifact.order, reverse=True)
    incomplete_matches.sort(key=lambda artifact: artifact.order, reverse=True)
    completed_or_candidate = promoted_matches + candidate_matches
    if incomplete_matches and (
        not completed_or_candidate
        or incomplete_matches[0].order > max(artifact.order for artifact in completed_or_candidate)
    ):
        raise LookupError(f"matching-review-incomplete:{incomplete_matches[0].path.parent}")
    # A candidate beside a promoted result in one run is still pending and blocks findings intake.
    if candidate_matches and (not promoted_matches or candidate_matches[0].order >= promoted_matches[0].order):
        raise LookupError(f"matching-review-candidate-unpromoted:{candidate_matches[0].path}")
    if closed_matches and (not matches or closed_matches[0].order > matches[0].order):
        raise LookupError("matching-review-closed-not-remediable")
    if invalid_matches and (not matches or max(artifact.order for artifact in invalid_matches) > matches[0].order):
        raise LookupError("invalid-review-report-rerun-code-review")
    if not matches:
        if unavailable_matches:
            raise LookupError("matching-review-unavailable-rerun-code-review")
        if invalid_matches:
            raise LookupError("invalid-review-report-rerun-code-review")
        raise LookupError("missing-matching-review-report")
    return matches[0].path


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
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
        if completed.returncode:
            diagnostic = (completed.stderr or completed.stdout).strip()
            raise LookupError(f"review-validation-failed:{diagnostic}")
    payload = json.loads(result_bytes)
    if result_path.read_bytes() != result_bytes:
        raise LookupError("review-completion-result-changed")
    return payload


def complete_review_run(run_dir: Path, *, codex_home: Path | None = None, parent_thread_id: str | None = None) -> bytes:
    """Emit only a validated promoted review whose PR handoff survives consumer discovery.

    This read-only boundary never repairs or promotes artifacts. Intake shares its validators, but only new producer
    completion requires bound schema-v3 final bytes for every disposition. Historical reports remain readable as
    unverified archives, not completion or remediation evidence.
    """
    run_dir = run_dir.resolve()
    result_path = run_dir / "result.json"
    if not result_path.is_file():
        raise LookupError(f"review-result-not-promoted:{run_dir}")
    result_bytes = result_path.read_bytes()
    payload = validate_review_result(result_path, codex_home=codex_home, parent_thread_id=parent_thread_id)
    if payload.get("schema_version") != 3:
        raise LookupError("review-completion-requires-bound-schema-v3")
    metadata = payload["metadata"]
    if metadata.get("scope") == "pr" and review_result_kind(result_path) == "assessed":
        identity = _read_pr_identity(run_dir / "pr.json")
        if identity is None:
            raise LookupError("review-completion-missing-pr-identity")
        reports_root = run_dir.parent.parent if PR_DIRECTORY_PATTERN.fullmatch(run_dir.parent.name) else run_dir.parent
        reports_dirs = [reports_root]
        current_root = CURRENT_REPORTS_DIR.resolve()
        legacy_root = LEGACY_REPORTS_DIR.resolve()
        if reports_root == current_root:
            reports_dirs.append(legacy_root)
        elif reports_root == legacy_root:
            reports_dirs.append(current_root)
        selected = find_latest_review_report(identity[1] or identity[0], reports_dirs)
        if selected.resolve() != result_path:
            raise LookupError(f"review-completion-not-current:{selected}")
    if result_path.read_bytes() != result_bytes:
        raise LookupError("review-completion-result-changed")
    final_bytes = (run_dir / "final.md").read_bytes()
    if hashlib.sha256(final_bytes).hexdigest() != metadata["final_handoff"]["rendered_sha256"]:
        raise LookupError("review-completion-final-changed")
    return final_bytes


def main(argv: list[str] | None = None) -> int:
    """Select a report or run the producer's fail-closed completion gate."""
    parser = argparse.ArgumentParser(description="Print the newest Codex code-review result matching a PR target.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--target", help="PR number, #number, or PR URL.")
    source.add_argument("--result", type=Path, help="Explicit result path that must be an assessed review.")
    source.add_argument(
        "--archive-result", type=Path, help="Describe a schema-2 or schema-3 result as unverified archive data."
    )
    source.add_argument("--complete-run", type=Path, help="Validate a promoted run and emit only its bound final text.")
    parser.add_argument("--codex-home", type=Path, help="Completion or intake validator's rollout-log root.")
    parser.add_argument("--parent-thread-id", help="Completion or intake validator's producer thread identity.")
    parser.add_argument(
        "--reports-dir",
        default=CURRENT_REPORTS_DIR,
        type=Path,
        help="Directory containing PR-scoped runs or legacy timestamped code-review reports.",
    )
    args = parser.parse_args(argv)

    try:
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
                    args.result, codex_home=args.codex_home, parent_thread_id=args.parent_thread_id
                )
            )
            return 0
        reports_dirs = [args.reports_dir]
        if args.reports_dir == CURRENT_REPORTS_DIR:
            reports_dirs.append(LEGACY_REPORTS_DIR)
        selected = find_latest_review_report(args.target, reports_dirs)
        print(
            require_assessed_review_result(selected, codex_home=args.codex_home, parent_thread_id=args.parent_thread_id)
        )
    except (LookupError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
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
