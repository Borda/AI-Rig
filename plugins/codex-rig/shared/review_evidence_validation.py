"""Validate the review, adversarial-loop and audit evidence inside Codex Rig workflow results.

## Purpose

Reject a code-review final handoff, an adversarial-loop ledger or an audit evidence set that is incomplete,
contradictory or unsupported before the result that cites it can be presented as complete.

## Scope

It reads local result, handoff, ledger and cost artifacts and raises ``SystemExit`` with a stable error code; it does
not run checks, review code or change any file. The shared JSON-object loader lives here because every validator in
``validate-artifacts.py`` reads artifacts through it.

## Usage

Import it from ``validate-artifacts.py``, which owns the command line; it is not an entry point.

## Outputs

It returns nothing on success and raises ``SystemExit`` carrying the missing or contradictory evidence code on failure.

## Failure

Any malformed, absent or inconsistent artifact fails closed with a stable code; nothing is downgraded to a warning.

## Used by

``validate-artifacts.py`` (the ``code-review``, ``loop`` and ``audit`` validation steps and the final-handoff check).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
from html import escape
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from adversarial_loop import SCHEMA_VERSION as LOOP_LEDGER_SCHEMA_VERSION
from adversarial_loop import load_ledger
from audit_consolidation import settle_consolidation
from create_run import PR_URL_PATTERN, same_pr_url
from final_handoff import (
    CLOSE_CODES,
    CLOSED_REVIEW_PRESENTATION,
    HandoffError,
    challenge_report_path,
    inline_text,
    literal_code_span,
    review_commit_sha,
)

#: Failure for a duplicate decision written before duplicates named their target, with the repair that resumes it.
DUPLICATE_SCHEMA_REPAIR = (
    "duplicate-decision-schema-v2-required: set close_decision.schema_version to 2 and close_decision.duplicate_of "
    "to the canonical GitHub URL of the earlier change already cited as an evidence source, then finalize again"
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit(f"expected-json-object:{path}")
    return payload


def _code_review_pr_ci_snapshot(rollup: object) -> str:
    """Summarize collected PR checks using failure, pending, then passing precedence."""
    if rollup is None or rollup == []:
        return "unavailable"
    if not isinstance(rollup, list):
        raise SystemExit("code-review-final-handoff-pr-snapshot-source-invalid")
    failed: list[str] = []
    pending: list[str] = []
    for check in rollup:
        if not isinstance(check, dict):
            raise SystemExit("code-review-final-handoff-pr-snapshot-source-invalid")
        if check.get("__typename") == "CheckRun":
            name = check.get("name")
            status = check.get("status")
            conclusion = check.get("conclusion")
            is_pending = status != "COMPLETED"
            is_failed = not is_pending and conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED"}
        elif check.get("__typename") == "StatusContext":
            name = check.get("context")
            state = check.get("state")
            is_pending = state not in {"SUCCESS", "FAILURE", "ERROR"}
            is_failed = state in {"FAILURE", "ERROR"}
        else:
            raise SystemExit("code-review-final-handoff-pr-snapshot-source-invalid")
        if not isinstance(name, str) or not name:
            raise SystemExit("code-review-final-handoff-pr-snapshot-source-invalid")
        if is_failed:
            failed.append(name)
        elif is_pending:
            pending.append(name)
    if failed:
        return f"failing — {', '.join(failed)}"
    if pending:
        return f"pending — {', '.join(pending)}"
    return "passing"


def _usable_refreshed_check(check: object) -> bool:
    """Require the GitHub fields needed to distinguish a finished check from one still running."""
    if not isinstance(check, dict):
        return False
    if check.get("__typename") == "CheckRun":
        status, conclusion = check.get("status"), check.get("conclusion")
        fields = [check.get("name"), status]
        if status == "COMPLETED":
            fields.append(conclusion)
        elif conclusion is not None and not isinstance(conclusion, str):
            return False
    elif check.get("__typename") == "StatusContext":
        fields = [check.get("context"), check.get("state")]
    else:
        return False
    return all(isinstance(field, str) and field.strip() for field in fields)


def reviewed_pr_ci_rollup(out_dir: Path, pr: dict[str, Any], reviewed_head: str | None) -> object:
    """Select the latest usable rollup for the collected and reviewed head, preserving the original collection."""
    collected = pr.get("statusCheckRollup")
    refresh_path = out_dir / "pr-ci-refresh.json"
    if not reviewed_head or pr.get("headRefOid") != reviewed_head or refresh_path.is_symlink():
        return collected
    try:
        if not refresh_path.is_file():
            return collected
        refresh = json.loads(refresh_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A partial response cannot displace the collector's retained observation.
        return collected
    if isinstance(refresh, dict) and refresh.get("headRefOid") == reviewed_head:
        rollup = refresh.get("statusCheckRollup")
        if isinstance(rollup, list) and rollup and all(_usable_refreshed_check(check) for check in rollup):
            return rollup
    return collected


def _display_value(value: object) -> object:
    """Normalize every string inside a decision value to inline Markdown, keeping its shape and non-string leaves."""
    if isinstance(value, str):
        return inline_text(value)
    if isinstance(value, list):
        return [_display_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _display_value(item) for key, item in value.items()}
    return value


def _display_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Return the only view of a close decision that may be rendered into review notes or a final response.

    Every model- or contributor-supplied string, including fields added later, becomes one inline paragraph line that
    cannot open a heading, list, quote, table, fence or HTML block. Identity comparisons keep using the raw decision.
    """
    return {key: _display_value(value) for key, value in decision.items()}


def closed_review_handoff(out_dir: Path, decision: dict[str, Any]) -> dict[str, Any]:
    """Derive the rejection, its reason and any duplicate's maintainer action from the verified PR and decision.

    Every close code recommends Reject with the close reason and reviewed commit. A duplicate also gets one maintainer
    action linking the declared earlier change, followed by the target's evidence of the overlap. Decision free text is
    taken from the normalized display view, so the writer, both validators and the renderer compare the same values, and
    a value that displays as nothing cannot stand in for required evidence.
    """
    code = decision.get("code")
    if not isinstance(code, str) or code not in CLOSE_CODES:
        raise SystemExit("closed-review-invalid-code")
    duplicate = code == "DUPLICATE"
    if type(decision.get("schema_version")) is not int or decision["schema_version"] != (2 if duplicate else 1):
        raise SystemExit(DUPLICATE_SCHEMA_REPAIR if duplicate else "closed-review-invalid-decision-policy")
    if decision.get("advisory_only") is not True:
        raise SystemExit("closed-review-invalid-decision-policy")
    view = _display_decision(decision)
    for field in ("summary", "rationale"):
        if not isinstance(view.get(field), str) or not view[field]:
            raise SystemExit(f"closed-review-missing-{field}")
    try:
        pr = _load_json(out_dir / "pr.json")
        routing = _load_json(out_dir / "pr-routing.json")
    except (OSError, ValueError) as error:
        raise SystemExit("closed-review-current-pr-invalid") from error
    current_url = pr.get("url")
    current = PR_URL_PATTERN.fullmatch(current_url) if isinstance(current_url, str) else None
    if (
        not current
        or type(pr.get("number")) is not int
        or str(pr["number"]) != current["number"]
        or not same_pr_url(current_url, routing.get("pr_url"))
        or type(routing.get("pr_number")) is not int
        or routing["pr_number"] != pr["number"]
    ):
        raise SystemExit("closed-review-current-pr-invalid")
    head_sha = decision.get("head_sha")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise SystemExit("closed-review-invalid-head-sha")
    if head_sha != pr.get("headRefOid") or head_sha != routing.get("head_oid"):
        raise SystemExit("closed-review-head-sha-mismatch")
    handoff: dict[str, Any] = {
        "presentation_version": CLOSED_REVIEW_PRESENTATION,
        "outcome": {"title": "Review Decision", "summary": "Recommendation: Reject."},
        "close_reason": {
            "code": code,
            "summary": view["summary"],
            "rationale": view["rationale"],
            "pr_url": current_url,
            "head_sha": head_sha,
        },
        "remaining": [],
        "next_steps": [],
    }
    evidence, shown_evidence = decision.get("evidence"), view.get("evidence")
    if not isinstance(evidence, list) or any(
        not isinstance(item, dict)
        or not all(isinstance(item.get(key), str) and item[key] for key in ("claim", "source"))
        for item in shown_evidence
    ):
        # The notes cite every entry; a value that displays as nothing cannot stand in for one.
        raise SystemExit("closed-review-empty-evidence")
    shown_counterevidence = view.get("counterevidence_checked")
    if (
        not isinstance(shown_counterevidence, list)
        or not shown_counterevidence
        or not all(isinstance(item, str) and item for item in shown_counterevidence)
    ):
        raise SystemExit("closed-review-counterevidence-required")
    if not duplicate:
        return handoff
    target = decision.get("duplicate_of")
    upstream = re.fullmatch(
        r"https://github[.]com/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*)/"
        r"(?:(?P<kind>pull|issues)/(?P<number>[1-9][0-9]*)|commit/(?P<sha>[0-9a-f]{40}))",
        target if isinstance(target, str) else "",
    )
    if not upstream:
        raise SystemExit("duplicate-target-invalid")
    current_repo = f"{current['owner']}/{current['repository']}"
    if upstream["number"] == current["number"] and upstream["repo"].casefold() == current_repo.casefold():
        # GitHub shares issue and pull request numbers and redirects /issues/<PR number> to the same PR.
        raise SystemExit("duplicate-target-self")
    # Identity compares raw sources; the displayed claims come from the normalized view.
    claims = [shown["claim"] for item, shown in zip(evidence, shown_evidence) if item["source"] == target]
    if not claims or any(not re.sub(r"https?://\S+", "", claim).strip() for claim in claims):
        raise SystemExit("duplicate-target-evidence-missing")
    if not any(item["source"] != target for item in evidence):
        # The target's own claims move into the maintainer action, so the notes need independent evidence to cite.
        raise SystemExit("closed-review-distinct-evidence-required")
    repo_label = "" if upstream["repo"].casefold() == current_repo.casefold() else upstream["repo"]
    # The decision proves the overlap, not the target's merge or resolution state, so the label names only its kind.
    if upstream["number"]:
        kind = "PR" if upstream["kind"] == "pull" else "issue"
        reference = f"{kind} {repo_label}#{upstream['number']}"
    else:
        sha = upstream["sha"][:12]
        reference = f"commit {repo_label}@{sha}" if repo_label else f"commit {sha}"
    action = f"Close [PR #{current['number']}]({current_url}) as a duplicate of [{reference}]({target})."
    # The rationale already follows the rejection; the action carries only the target's evidence of the overlap.
    overlap = inline_text(" ".join(dict.fromkeys(claims)))
    handoff["remaining"] = [{"row_id": "duplicate", "owner": "Maintainer", "item": overlap, "next_action": action}]
    handoff["next_steps"] = ["duplicate"]
    return handoff


def validate_closed_review_handoff(expected: dict[str, Any], handoff: dict[str, Any]) -> None:
    """Bind the chosen action, recommendation, evidence and owner to the retained proposal decision."""
    if handoff.get("branch") != "closed" or any(handoff.get(key) != value for key, value in expected.items()):
        raise SystemExit("closed-review-final-handoff-mismatch")


def closed_review_notes(decision: dict[str, Any], *, current_handoff: dict[str, Any] | None = None) -> str:
    """Render canonical close-only notes with the reviewed evidence and, for a duplicate, the maintainer action.

    Without ``current_handoff`` this reproduces the historical close notes byte for byte, so earlier promoted runs
    replay and canonical earlier notes can be upgraded. The current form opens with the rejection table and reads every
    free-text value from the normalized display view, so no field can open a Markdown block. Each evidence source is a
    literal code span; the close code, PR URL, head and duplicate action come from the validated handoff.
    """
    if not current_handoff:
        evidence = "\n".join(f"- `{item['source']}`: {item['claim']}" for item in decision["evidence"])
        counterevidence = "\n".join(f"- {item}" for item in decision["counterevidence_checked"])
        return (
            "# Review Decision: close\n\n"
            "Source findings: not assessed\n\n"
            "Detailed review: skipped\n\n"
            f"Close reason: `{decision['code']}`\n\n"
            f"Summary: {decision['summary']}\n\n"
            f"Rationale: {decision['rationale']}\n\n"
            f"Evidence:\n\n{evidence}\n\n"
            f"Counterevidence checked:\n\n{counterevidence}\n\n"
            "GitHub mutation: not performed."
        )
    reason = current_handoff["close_reason"]
    view = _display_decision(decision)
    duplicate_action = current_handoff["remaining"][0] if reason["code"] == "DUPLICATE" else None
    # A duplicate's target evidence appears once, in its maintainer action; identity compares the raw source.
    evidence = "\n".join(
        f"- {literal_code_span(shown['source'])}: {shown['claim']}"
        for item, shown in zip(decision["evidence"], view["evidence"])
        if not duplicate_action or item["source"] != decision["duplicate_of"]
    )
    counterevidence = "\n".join(f"- {item}" for item in view["counterevidence_checked"])
    next_steps = ""
    if duplicate_action:
        next_steps = f"\n\n## Next steps\n\n- Maintainer: {duplicate_action['next_action']} {duplicate_action['item']}"
    return (
        "# Review Decision\n\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        "| Recommendation | Reject |\n"
        f"| Close reason | `{reason['code']}` |\n"
        f"| PR | [View pull request]({reason['pr_url']}) |\n"
        f"| Head | `{reason['head_sha']}` |\n\n"
        "Source findings: not assessed\n\n"
        "Detailed review: skipped\n\n"
        f"Summary: {view['summary']}\n\n"
        f"Rationale: {view['rationale']}\n\n"
        f"Evidence:\n\n{evidence}\n\n"
        f"Counterevidence checked:\n\n{counterevidence}\n\n"
        f"GitHub mutation: not performed.{next_steps}"
    )


def _validate_code_review_final_handoff(
    result: dict[str, Any],
    handoff: dict[str, Any],
    *,
    candidate: bool = False,
    out_dir: Path | None = None,
    fresh_candidate: bool = False,
) -> None:
    """Bind review branches and retained attribution while requiring compact presentation for fresh candidates."""
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise SystemExit("code-review-final-handoff-metadata-invalid")
    review_status = metadata.get("review_status")
    expected_branch = review_status if review_status in {"unavailable", "closed"} else "assessed"
    close_decision = metadata.get("close_decision")
    if handoff.get("presentation_version") == 17 and expected_branch != "closed":
        raise SystemExit("closed-review-final-handoff-decision-required")
    if expected_branch == "closed" and (
        fresh_candidate
        or handoff.get("presentation_version") == 17
        or (isinstance(close_decision, dict) and close_decision.get("schema_version") == 2)
    ):
        if not isinstance(close_decision, dict) or out_dir is None:
            raise SystemExit("closed-review-final-handoff-decision-required")
        validate_closed_review_handoff(closed_review_handoff(out_dir, close_decision), handoff)
    if handoff.get("branch") == "caller-contract" and result.get("schema_version") != 3:
        return
    if handoff.get("branch") != expected_branch:
        raise SystemExit("code-review-final-handoff-branch-mismatch")
    if result.get("schema_version") == 3 and expected_branch == "assessed":
        if fresh_candidate and handoff.get("presentation_version") not in {8, 9, 10, 11, 12, 13, 14, 15, 16}:
            raise SystemExit("review-current-handoff-presentation-v8-required")
        if handoff.get("presentation_version") not in {3, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}:
            raise SystemExit("review-current-handoff-presentation-invalid")
    if expected_branch == "assessed":
        decision = metadata.get("review_decision")
        recommendation = decision.get("recommendation") if isinstance(decision, dict) else None
        expected_outcome = {
            "title": "Review Decision",
            "summary": f"Recommendation: {recommendation}.",
        }
        if handoff.get("outcome") != expected_outcome:
            raise SystemExit("code-review-final-handoff-outcome-mismatch")
    tables = handoff.get("tables")
    if not isinstance(tables, list):
        raise SystemExit("code-review-final-handoff-tables-invalid")
    tables_by_heading = {
        table.get("heading"): table
        for table in tables
        if isinstance(table, dict) and isinstance(table.get("heading"), str)
    }
    headings = set(tables_by_heading)
    if result.get("schema_version") == 3 and expected_branch == "assessed":
        expected_snapshot = "PR Snapshot" if metadata.get("scope") == "pr" else "Review Snapshot"
        if headings & {"PR Snapshot", "Review Snapshot"} != {expected_snapshot}:
            raise SystemExit("code-review-final-handoff-snapshot-scope-mismatch")
    reviewer_tables = [table for table in tables if isinstance(table, dict) and "reviewers" in table]
    assessments = metadata.get("reviewer_assessments")
    if (candidate or result.get("schema_version") == 3) and expected_branch == "assessed":
        snapshot_heading = "PR Snapshot" if metadata.get("scope") == "pr" else "Review Snapshot"
        if (
            not isinstance(assessments, list)
            or not assessments
            or len(reviewer_tables) != 1
            or reviewer_tables[0].get("heading") != snapshot_heading
            or reviewer_tables[0]["reviewers"] != assessments
        ):
            raise SystemExit("code-review-final-handoff-candidate-attribution-missing")
    if assessments is not None or reviewer_tables:
        if expected_branch != "assessed" or len(reviewer_tables) != 1 or reviewer_tables[0]["reviewers"] != assessments:
            raise SystemExit("code-review-final-handoff-reviewers-mismatch")
        if reviewer_tables[0].get("summary") != metadata["review_decision"].get("summary"):
            raise SystemExit("code-review-final-handoff-review-summary-mismatch")
        validation = metadata.get("review_validation")
        if validation is not None and (
            reviewer_tables[0].get("source_rating_version") != validation.get("source_rating_version")
            or reviewer_tables[0].get("verification_readiness") != validation.get("verification_readiness")
        ):
            raise SystemExit("code-review-final-handoff-source-rating-version-mismatch")
        if validation is not None:
            remaining = {item.get("row_id"): item for item in handoff.get("remaining", [])}
            for concern in validation.get("candidates", []):
                if concern.get("disposition") not in {None, "unverified"}:
                    continue
                action = remaining.get(concern["id"], {})
                if (
                    action.get("owner") != concern["owner"]
                    or concern["claim"] not in action.get("item", "")
                    or concern["missing_prerequisite"] not in action.get("item", "")
                    or concern["next_check"] not in action.get("next_action", "")
                    or concern["closure_condition"] not in action.get("next_action", "")
                ):
                    raise SystemExit(f"code-review-final-handoff-open-concern-missing:{concern['id']}")
    if expected_branch in {"unavailable", "closed"} and tables:
        raise SystemExit("code-review-terminal-final-handoff-table-forbidden")
    if expected_branch == "assessed" and metadata.get("scope") == "pr" and "PR Snapshot" not in headings:
        raise SystemExit("code-review-final-handoff-pr-snapshot-missing")
    # `Review Snapshot` is the non-PR counterpart and carries the reviewed target and revision where the PR
    # snapshot names the pull request. The heading is new in this release, so no stored artifact predates it.
    snapshot_fields = {
        "PR Snapshot": ("PR", "Author", "CI", "Type", "Suggestion"),
        "Review Snapshot": ("Scope", "Revision", "CI", "Type", "Suggestion"),
    }
    snapshot_heading = next((heading for heading in snapshot_fields if heading in headings), None)
    if expected_branch == "assessed" and snapshot_heading:
        label = "pr-snapshot" if snapshot_heading == "PR Snapshot" else "review-snapshot"
        snapshot = tables_by_heading[snapshot_heading]
        rows = snapshot.get("rows")
        expected_fields = snapshot_fields[snapshot_heading]
        reviewed_commit = metadata.get("reviewed_commit_sha")
        if reviewed_commit is not None:
            expected_fields = (*expected_fields[:-1], "Commit", expected_fields[-1])
        if (
            not isinstance(rows, list)
            or tuple(
                row["cells"][0]
                if isinstance(row, dict) and isinstance(row.get("cells"), list) and len(row["cells"]) == 2
                else None
                for row in rows
            )
            != expected_fields
        ):
            raise SystemExit(f"code-review-final-handoff-{label}-fields-mismatch")
        if fresh_candidate and reviewed_commit is None:
            raise SystemExit("review-commit-missing")
        if reviewed_commit is not None:
            if out_dir is None:
                raise SystemExit("review-commit-source-missing")
            try:
                retained_commit = review_commit_sha(out_dir, metadata.get("scope"))
            except HandoffError as error:
                raise SystemExit(str(error)) from error
            if reviewed_commit != retained_commit:
                raise SystemExit("review-commit-metadata-mismatch")
            expected_commit = (
                f"{retained_commit} (working-tree base)"
                if metadata.get("scope") in {"working-tree", "path"}
                else retained_commit
            )
            if rows[-2]["cells"][1] != expected_commit:
                raise SystemExit("review-commit-row-mismatch")
        decision = metadata.get("review_decision")
        recommendation = decision.get("recommendation") if isinstance(decision, dict) else None
        suggestions = {
            "accept-as-is": "approve",
            "minor-changes": "minor changes",
            "needs-more-work": "needs work",
            "insufficient-but-promising": "insufficient, but a good initiative",
            "reject": "reject",
            "not-aligned": "not aligned",
            None: "no source judgment",
        }
        if metadata.get("review_validation") is not None:
            # Source-rated reviews: a version 1 name and its version 2 token render the same Suggestion cell.
            suggestions.update(
                {
                    "needs-more-work": "major revision",
                    "reject": "block",
                    "APPROVE": "approve",
                    "MINOR_EDITS": "minor changes",
                    "MAJOR_REVISION": "major revision",
                    "INSUFFICIENT_BUT_PROMISING": "insufficient, but a good initiative",
                    "REJECT": "block",
                }
            )
        suggestion_cells = rows[-1].get("cells")
        if (
            not isinstance(suggestion_cells, list)
            or len(suggestion_cells) != 2
            or suggestion_cells[1] != suggestions.get(recommendation)
        ):
            raise SystemExit(f"code-review-final-handoff-{label}-suggestion-mismatch")
        if snapshot_heading == "Review Snapshot" and result.get("schema_version") == 3:
            if rows[0]["cells"][1] != metadata.get("scope"):
                raise SystemExit("code-review-final-handoff-review-snapshot-scope-mismatch")
            if out_dir is None:
                raise SystemExit("code-review-final-handoff-review-snapshot-source-invalid")
            try:
                diff_digest = hashlib.sha256((out_dir / "diff.patch").read_bytes()).hexdigest()
            except OSError as exception:
                raise SystemExit("code-review-final-handoff-review-snapshot-source-invalid") from exception
            if metadata.get("review_input_sha256") != diff_digest:
                raise SystemExit("code-review-final-handoff-review-snapshot-source-invalid")
            if rows[1]["cells"][1] != f"diff sha256:{diff_digest}":
                raise SystemExit("code-review-final-handoff-review-snapshot-revision-mismatch")
            # A local review has no retained remote CI run. Keep its status explicit.
            if rows[2]["cells"][1] != "unavailable":
                raise SystemExit("code-review-final-handoff-review-snapshot-ci-mismatch")
            if rows[3]["cells"][1] not in {"fix", "feat", "refactor", "perf", "docs", "ci", "chore", "test", "mixed"}:
                raise SystemExit("code-review-final-handoff-review-snapshot-type-invalid")
        if candidate and snapshot_heading == "PR Snapshot" and out_dir is not None:
            pr = _load_json(out_dir / "pr.json")
            number = pr.get("number")
            title = pr.get("title")
            url = pr.get("url")
            author = pr.get("author")
            login = author.get("login") if isinstance(author, dict) else None
            if (
                type(number) is not int
                or number < 1
                or not isinstance(title, str)
                or not title.strip()
                or not isinstance(url, str)
                or not url.strip()
                or not isinstance(login, str)
                or not login.strip()
            ):
                raise SystemExit("code-review-final-handoff-pr-snapshot-source-invalid")
            expected_values = (
                f"[#{number} — {escape(title, quote=False).replace('[', '&#91;').replace(']', '&#93;')}]({url})",
                f"@{login}",
                _code_review_pr_ci_snapshot(reviewed_pr_ci_rollup(out_dir, pr, reviewed_commit)),
            )
            types = {"fix", "feat", "refactor", "perf", "docs", "ci", "chore", "test", "mixed"}
            if tuple(row["cells"][1] for row in rows[:3]) != expected_values or rows[3]["cells"][1] not in types:
                raise SystemExit("code-review-final-handoff-pr-snapshot-value-mismatch")
    finding_total = sum(result["findings"].values())
    if expected_branch == "assessed" and finding_total and "Review Findings and Merge Blocks" not in headings:
        raise SystemExit("code-review-final-handoff-findings-table-missing")
    if expected_branch == "assessed" and result.get("schema_version") in {2, 3}:
        records = metadata.get("review_findings")
        blockers = metadata.get("operational_blockers", [])
        if not isinstance(records, list) or not isinstance(blockers, list):
            raise SystemExit("code-review-final-handoff-finding-records-missing")
        if metadata.get("finding_records_version") not in {1, 2}:
            raise SystemExit("code-review-final-handoff-records-version-missing")
        identities = [record.get("id") if isinstance(record, dict) else None for record in records + blockers]
        if any(not isinstance(identity, str) or not identity.strip() for identity in identities):
            raise SystemExit("code-review-final-handoff-finding-records-invalid")
        table = tables_by_heading.get("Review Findings and Merge Blocks", {})
        if identities and table.get("layout") not in {"grouped", "concise"}:
            raise SystemExit("code-review-final-handoff-grouped-layout-required")
        rows = table.get("rows", [])
        row_identities = [
            row["cells"][0] if isinstance(row, dict) and isinstance(row.get("cells"), list) and row["cells"] else None
            for row in rows
        ]
        if len(row_identities) != len(identities) or set(row_identities) != set(identities):
            raise SystemExit("code-review-final-handoff-finding-identity-mismatch")
        if table.get("layout") in {"grouped", "concise"}:
            by_id = {record["id"]: record for record in records + blockers}
            for row in rows:
                record = by_id[row["cells"][0]]
                if row.get("title") != record.get("title", record["id"]):
                    raise SystemExit("code-review-final-handoff-finding-title-mismatch")
                for field in ("summary", "closure_evidence"):
                    if row.get(field) != record.get(field):
                        raise SystemExit(f"code-review-final-handoff-finding-{field}-mismatch")
                if row.get("authors") != record.get("authors"):
                    raise SystemExit("code-review-final-handoff-finding-authors-mismatch")
                if "required_change" in record and row["cells"][1:3] != [
                    record["required_change"],
                    "; ".join(record["evidence"]),
                ]:
                    raise SystemExit("code-review-final-handoff-finding-content-mismatch")


def _validate_adversarial_loop(
    result: dict[str, Any],
    out_dir: Path,
    gates: dict[str, Any],
    *,
    candidate: bool,
    current_result: bool = False,
    repository: Path | None = None,
) -> None:
    """Bind the loop decision to retained snapshots, reports, and visible result rows."""
    if result.get("schema_version") != 2:
        raise SystemExit("adversarial-loop-schema-v2-required")
    table_version = result.get("metadata", {}).get("challenge_table_contract_version")
    if table_version is None:
        raise SystemExit("adversarial-loop-table-contract-required")
    if type(table_version) is not int:
        raise SystemExit("adversarial-loop-table-contract-version-invalid")
    if table_version not in {1, 2}:
        raise SystemExit("adversarial-loop-table-contract-version-required")
    tables = _load_json(out_dir / "final-handoff.json")["tables"]
    if any((table.get("layout") == "challenge-history") != (table_version == 2) for table in tables):
        raise SystemExit("adversarial-loop-table-contract-version-required")
    scope = result["metadata"].get("challenge_scope")
    if "child_runs" in _load_json(out_dir / "final-handoff.json") and not (
        isinstance(scope, dict) and scope.get("mode") in ("chunked", "scoped")
    ):
        raise SystemExit("adversarial-loop-child-scores-scope-invalid")
    if isinstance(scope, dict) and scope.get("mode") in ("chunked", "scoped"):
        _validate_chunked_coordinator(
            result, out_dir, gates, candidate=candidate, current_result=current_result, repository=repository
        )
        return
    if candidate or "challenge_scope" in result["metadata"]:
        _validate_challenge_scope(result["metadata"], out_dir, repository=repository)
    ledger_path = out_dir / "loop-ledger.json"
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        [sys.executable, str(Path(__file__).with_name("adversarial_loop.py")), "--ledger", str(ledger_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise SystemExit("adversarial-loop-invalid-ledger:" + completed.stderr.strip())
    # Historical schema-1 ledgers rewrote every round inline; a current result keeps rounds in the append-only log.
    ledger = load_ledger(ledger_path)
    if ledger["schema_version"] != LOOP_LEDGER_SCHEMA_VERSION:
        raise SystemExit("adversarial-loop-ledger-schema-v2-required")
    action_contract = result.get("metadata", {}).get("action_contract_version")
    # Actions schema 3 names an invariant on every action; schema-1 and schema-2 records stay archives only.
    if type(action_contract) is not int or action_contract != 3:
        raise SystemExit("adversarial-loop-action-contract-required")
    actions_path = out_dir / "loop-actions.json"
    actions = subprocess.run(  # noqa: S603 - argv list, no shell
        [
            sys.executable,
            str(Path(__file__).with_name("adversarial_loop.py")),
            "--ledger",
            str(ledger_path),
            "--actions",
            str(actions_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if actions.returncode:
        raise SystemExit("adversarial-loop-invalid-actions:" + actions.stderr.strip())
    if _load_json(actions_path).get("schema_version") != action_contract:
        raise SystemExit("adversarial-loop-action-contract-required")
    summary = json.loads(completed.stdout)
    if result.get("metadata", {}).get("adversarial_loop") != summary:
        raise SystemExit("adversarial-loop-summary-mismatch")
    if result["status"] == "pass" and summary["reason"] != "clean":
        raise SystemExit("adversarial-loop-incomplete-review")
    review_gate = next(check for check in gates["checks"] if check["id"] == "review")
    if summary["reason"] != "clean" and (review_gate["status"] != "fail" or "review" not in result["checks_failed"]):
        raise SystemExit("adversarial-loop-nonclean-review-gate")
    snapshots = [("current.diff", ledger["current_snapshot"])]
    snapshots.extend((f"round-{item['index']}.diff", item["snapshot"]) for item in ledger["rounds"])
    for filename, snapshot in snapshots:
        path = out_dir / filename
        if not path.is_file() or not path.resolve().is_relative_to(out_dir.resolve()):
            raise SystemExit(f"adversarial-loop-snapshot-missing:{filename}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != snapshot["diff_digest"]:
            raise SystemExit(f"adversarial-loop-snapshot-digest-mismatch:{filename}")
    for item in ledger["rounds"]:
        report = out_dir / item["report_path"]
        if not report.resolve().is_relative_to(out_dir.resolve()) or not report.is_file():
            raise SystemExit("adversarial-loop-report-missing-or-outside-run")
        if not report.read_text(encoding="utf-8").strip():
            raise SystemExit("adversarial-loop-report-empty")
    counts = (
        summary["rounds"][-1]["counts"]
        if summary["rounds"]
        else dict.fromkeys(("security", "critical", "high", "medium", "low", "nit"), 0)
    )
    expected_findings = {
        "critical": counts["critical"] + counts["security"],
        "high": counts["high"],
        "medium": counts["medium"],
        "low": counts["low"] + counts["nit"],
    }
    if result["findings"] != expected_findings:
        raise SystemExit("adversarial-loop-finding-count-mismatch")
    _validate_adversarial_loop_rows(out_dir, ledger, summary, table_version=table_version)
    evidence_validator = (
        Path(__file__).resolve().parent.parent / "skills" / "challenge-resolve" / "validate_evidence.py"
    )
    evidence = subprocess.run(  # noqa: S603 - argv list, no shell
        [sys.executable, str(evidence_validator), "--out", str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if evidence.returncode:
        raise SystemExit("adversarial-loop-evidence-invalid:" + evidence.stderr.strip())
    loop_evidence = _load_json(out_dir / "loop-evidence.json")
    if (candidate or current_result) and (
        type(loop_evidence.get("schema_version")) is not int
        or loop_evidence["schema_version"] not in {2, 3}
        or "origin" not in loop_evidence
    ):
        raise SystemExit("adversarial-loop-evidence-contract-required")
    if "origin" in loop_evidence and result["metadata"].get("challenge_origin") != loop_evidence["origin"]:
        raise SystemExit("adversarial-loop-origin-mismatch")


def _validate_challenge_scope(
    metadata: dict[str, Any],
    out_dir: Path,
    *,
    summary: bool = False,
    stopped: bool = False,
    repository: Path | None = None,
) -> dict[str, Any] | None:
    """Recheck declared chunk coverage and return a clean or stopped child-backed summary."""
    scope = metadata.get("challenge_scope")
    if not isinstance(scope, dict):
        raise SystemExit("adversarial-loop-challenge-scope-required")
    chunks_dir = out_dir / "chunks"
    if scope == {"mode": "single"}:
        if chunks_dir.exists() or chunks_dir.is_symlink():
            raise SystemExit("adversarial-loop-challenge-scope-mismatch")
        return
    mode = scope.get("mode")
    if mode not in ("chunked", "scoped") or scope != {
        "mode": mode,
        "manifest_path": "chunks/chunks.json",
        "results_path": "chunks/results.json",
    }:
        raise SystemExit("adversarial-loop-challenge-scope-invalid")
    manifest = chunks_dir / "chunks.json"
    results = chunks_dir / "results.json"
    if (
        chunks_dir.is_symlink()
        or manifest.is_symlink()
        or results.is_symlink()
        or not manifest.is_file()
        or not results.is_file()
        or not manifest.resolve().is_relative_to(out_dir.resolve())
        or not results.resolve().is_relative_to(out_dir.resolve())
    ):
        raise SystemExit("adversarial-loop-chunk-coverage-invalid")
    # Prevent an old exhaustive result label from promoting a scoped manifest, or vice versa.
    try:
        manifest_value = json.loads(manifest.read_bytes())
    except (OSError, ValueError):
        manifest_value = None
    manifest_version = manifest_value.get("schema_version") if isinstance(manifest_value, dict) else None
    if type(manifest_version) is int and manifest_version in (1, 2):
        if manifest_version != {"chunked": 1, "scoped": 2}[mode]:
            raise SystemExit("adversarial-loop-chunk-coverage-invalid:scope-schema-mismatch")
    checker = Path(__file__).resolve().parent.parent / "skills" / "challenge-resolve" / "chunk_diff.py"
    checked = subprocess.run(  # noqa: S603 - argv list, no shell
        [
            sys.executable,
            str(checker),
            "check-stopped" if stopped else "check",
            "--manifest",
            str(manifest),
            "--results",
            str(results),
        ]
        + (["--repository", str(repository)] if repository is not None else [])
        + (["--summary"] if summary else []),
        capture_output=True,
        text=True,
        check=False,
    )
    if checked.returncode:
        raise SystemExit("adversarial-loop-chunk-coverage-invalid:" + checked.stderr.strip())
    if not summary:
        return None
    try:
        return json.loads(checked.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit("adversarial-loop-chunk-summary-invalid") from error


def _validate_scoped_coverage_limit(checked: dict[str, Any], handoff: dict[str, Any]) -> None:
    """Require the checked semantic-coverage limit in the human-visible handoff."""
    limit = checked.get("coverage_limit")
    confidence = handoff.get("confidence")
    remaining = handoff.get("remaining")
    if not isinstance(limit, str) or not limit.strip() or not isinstance(confidence, dict):
        raise SystemExit("adversarial-loop-scoped-coverage-limit-missing")
    limits = confidence.get("limits")
    if not isinstance(limits, list) or not isinstance(remaining, list):
        raise SystemExit("adversarial-loop-scoped-coverage-limit-missing")
    visible = [re.sub(r"^\(-0\.\d{2}\)\s*", "", item) for item in limits if isinstance(item, str)]
    visible.extend(item.get("item") for item in remaining if isinstance(item, dict))
    if limit not in visible:
        raise SystemExit("adversarial-loop-scoped-coverage-limit-missing")


def _validate_chunked_coordinator(
    result: dict[str, Any],
    out_dir: Path,
    gates: dict[str, Any],
    *,
    candidate: bool,
    current_result: bool = False,
    repository: Path | None = None,
) -> None:
    """Bind a clean or stopped coordinator to checked child evidence without an invented parent score."""
    recorded = result["metadata"].get("chunked_review")
    stopped = result["status"] == "fail" and isinstance(recorded, dict) and recorded.get("status") in {None, "stopped"}
    checked = _validate_challenge_scope(
        result["metadata"], out_dir, summary=True, stopped=stopped, repository=repository
    )
    if not isinstance(checked, dict) or not checked.get("runs"):
        raise SystemExit("adversarial-loop-chunk-summary-invalid")
    manifest_path = out_dir / "chunks" / "chunks.json"
    if manifest_path.is_file():
        manifest = _load_json(manifest_path)
        scope = result["metadata"].get("challenge_scope", {})
        mode = scope.get("mode", "chunked") if isinstance(scope, dict) else "chunked"
        expected_version = {"chunked": 1, "scoped": 2}.get(mode) if isinstance(mode, str) else None
        if current_result and (
            type(manifest.get("schema_version")) is not int or manifest["schema_version"] != expected_version
        ):
            raise SystemExit("adversarial-loop-chunk-origin-contract-required")
        if manifest.get("schema_version") in (1, 2) and result["metadata"].get("challenge_origin") != manifest.get(
            "origin"
        ):
            raise SystemExit("adversarial-loop-chunk-origin-mismatch")
    metadata = result["metadata"]
    if metadata.get("chunked_review") != checked or "adversarial_loop" in metadata:
        raise SystemExit("adversarial-loop-chunk-summary-mismatch")
    review_gate = next(check for check in gates["checks"] if check["id"] == "review")
    if stopped:
        if (
            result["status"] != "fail"
            or review_gate["status"] != "fail"
            or "review" not in result["checks_failed"]
            or any(result["findings"].values())
        ):
            raise SystemExit("adversarial-loop-chunk-coordinator-must-stop")
    elif (
        result["status"] != "pass"
        or review_gate["status"] != "pass"
        or result["checks_failed"]
        or any(result["findings"].values())
        or any(
            run.get("decision") != "clean" or not run.get("scores") or run["scores"][-1] != 0 for run in checked["runs"]
        )
    ):
        raise SystemExit("adversarial-loop-chunk-coordinator-clean-result-invalid")
    handoff = _load_json(out_dir / "final-handoff.json")
    scope = metadata.get("challenge_scope")
    if isinstance(scope, dict) and scope.get("mode") == "scoped":
        _validate_scoped_coverage_limit(checked, handoff)
    if candidate and handoff.get("presentation_version") not in {3, 8, 9, 10, 11, 12, 13, 14, 15}:
        raise SystemExit("adversarial-loop-child-scores-presentation-required")
    if (
        handoff.get("presentation_version") in {3, 8, 9, 10, 11, 12, 13, 14, 15}
        and handoff.get("child_runs") != checked["runs"]
    ):
        raise SystemExit("adversarial-loop-child-scores-mismatch")
    rows = [row["cells"] for table in handoff["tables"] for row in table["rows"]]
    if any(row.get("admission") == "advisory" for table in handoff["tables"] for row in table["rows"]):
        raise SystemExit("adversarial-loop-coordinator-advisory-invalid")
    if rows != [["not-run", *(["N/A"] * 7), "chunk-only", "chunks/results.json"]]:
        raise SystemExit("adversarial-loop-chunk-coordinator-table-mismatch")
    if stopped and (not handoff["remaining"] or not handoff["next_steps"]):
        raise SystemExit("adversarial-loop-recovery-guidance-missing")


def _validate_adversarial_loop_rows(
    out_dir: Path, ledger: dict[str, Any], summary: dict[str, Any], *, table_version: int
) -> None:
    """Keep displayed round scores and decisions equal to their ledger evidence."""
    handoff = _load_json(out_dir / "final-handoff.json")
    records = [row for table in handoff["tables"] for row in table["rows"]]
    advisory = [row for row in records if table_version == 2 and row.get("admission") == "advisory"]
    rows = [row["cells"] for row in records if row not in advisory]
    for row in advisory:
        challenge_report_path(out_dir, row["cells"][-1])
    expected = []
    seen: set[str] = set()
    weights = {"security": 20, "critical": 10, "high": 6, "medium": 4, "low": 2, "nit": 1}
    for item in summary["rounds"]:
        round_record = ledger["rounds"][item["index"] - 1]
        report = round_record["report_path"]
        counts = item["counts"]
        if table_version in {1, 2}:
            new_counts = dict.fromkeys(weights, 0)
            for finding in round_record["findings"]:
                if (
                    finding["disposition"] in {"open", "fixed-pending-verification"}
                    and finding["signature"] not in seen
                ):
                    new_counts[finding["tier"]] += 1
            new_score = sum(new_counts[tier] * weight for tier, weight in weights.items())
            expected.append(
                [
                    str(item["index"]),
                    *(f"{counts[tier] - new_counts[tier]} + {new_counts[tier]}" for tier in weights),
                    f"{item['score'] - new_score} + {new_score}",
                    item["decision"],
                    report,
                ]
            )
        seen.update(finding["signature"] for finding in round_record["findings"])
    if not expected and not advisory:
        expected = [["not-run", *(["N/A"] * 7), summary["reason"], "loop-report.md"]]
    if rows != expected:
        raise SystemExit("adversarial-loop-table-mismatch")
    if summary["reason"] != "clean" and (not handoff["remaining"] or not handoff["next_steps"]):
        raise SystemExit("adversarial-loop-recovery-guidance-missing")


def _audit_cost_artifact(out_dir: Path, record: object, field: str) -> Any:
    """Read digest-bound audit evidence without accepting paths outside the run."""
    if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
        raise SystemExit(f"audit-cost-invalid-evidence:{field}")
    raw_path = record["path"]
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        raise SystemExit(f"audit-cost-invalid-path:{field}")
    declared = PurePosixPath(raw_path)
    if declared.is_absolute() or PureWindowsPath(raw_path).drive or ".." in declared.parts:
        raise SystemExit(f"audit-cost-invalid-path:{field}")
    path = out_dir.resolve() / declared
    components = [out_dir.resolve().joinpath(*declared.parts[:index]) for index in range(1, len(declared.parts) + 1)]
    if any(component.is_symlink() for component in components) or not path.is_file():
        raise SystemExit(f"audit-cost-missing-evidence:{field}")
    if not path.resolve().is_relative_to(out_dir.resolve()):
        raise SystemExit(f"audit-cost-invalid-path:{field}")
    if record["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
        raise SystemExit(f"audit-cost-evidence-digest-mismatch:{field}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise SystemExit(f"audit-cost-invalid-json:{field}") from error


def _audit_cost_number(value: object, field: str, *, positive: bool = False) -> float:
    """Reject boolean, nonfinite, or negative cost and failure measurements."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise SystemExit(f"audit-cost-invalid-number:{field}")
    if value < 0 or (positive and value == 0):
        raise SystemExit(f"audit-cost-invalid-number:{field}")
    return float(value)


def _validate_audit_evidence(result: dict[str, Any], gates: dict[str, Any], out_dir: Path) -> None:
    """Require assessed audit completion and retained proof for optimization acceptance.

    Validation establishes evidence shape, digest integrity, matching identities, and declared guard outcomes. The
    workflow owner still verifies source completeness, reviewer independence, and provider provenance.
    """
    if result["status"] == "pass" and not any(
        check["id"] == "review" and check["status"] == "pass" for check in gates["checks"]
    ):
        raise SystemExit("audit-review-gate-required")
    consolidation = result["metadata"].get("consolidation")
    if result["metadata"].get("axis") == "consolidate" or consolidation is not None:
        if not isinstance(consolidation, dict):
            raise SystemExit("audit-consolidation-evidence-required")
        report = _audit_cost_artifact(out_dir, consolidation.get("report"), "consolidation")
        try:
            settled = settle_consolidation(report)
        except (OSError, ValueError) as error:
            raise SystemExit(f"audit-consolidation-invalid:{error}") from error
        if consolidation.get("outcomes") != settled["outcomes"]:
            raise SystemExit("audit-consolidation-settlement-mismatch")
        cost = result["metadata"].get("value_per_token")
        if isinstance(cost, dict) and cost.get("status") == "accepted":
            raise SystemExit("audit-consolidation-advisory-only")
    value = result["metadata"].get("value_per_token")
    if value is None:
        return
    if not isinstance(value, dict) or value.get("status") not in {
        "not-run",
        "insufficient-evidence",
        "rejected",
        "accepted",
    }:
        raise SystemExit("audit-cost-invalid-status")
    if value["status"] != "accepted":
        return
    if type(value.get("schema_version")) is not int or value["schema_version"] != 2:
        raise SystemExit("audit-cost-acceptance-schema-required")
    for field in ("scope_roots", "conditional_load_trace", "residual_limits"):
        if (
            not isinstance(value.get(field), list)
            or not value[field]
            or not all(isinstance(item, str) and item.strip() for item in value[field])
        ):
            raise SystemExit(f"audit-cost-missing-evidence:{field}")
    for field in ("baseline_sha256", "candidate_sha256"):
        if not isinstance(value.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", value[field]):
            raise SystemExit(f"audit-cost-invalid-digest:{field}")
    if value["baseline_sha256"] == value["candidate_sha256"]:
        raise SystemExit("audit-cost-unchanged-snapshot")
    if not isinstance(value.get("decision"), str) or not value["decision"].strip():
        raise SystemExit("audit-cost-missing-decision")
    measurements = value.get("static_measurements")
    if not isinstance(measurements, list) or not measurements:
        raise SystemExit("audit-cost-missing-measurements")
    for measurement in measurements:
        if not isinstance(measurement, dict) or measurement.get("source") not in {
            "provider-native",
            "tiktoken:o200k_base",
        }:
            raise SystemExit("audit-cost-token-source-required")
        for field in ("baseline_sha256", "candidate_sha256"):
            if measurement.get(field) != value[field]:
                raise SystemExit("audit-cost-measurement-snapshot-mismatch")
        for field in ("baseline_tokens", "candidate_tokens"):
            _audit_cost_number(measurement.get(field), field, positive=True)
    map_path = value.get("obligation_map_path")
    if not isinstance(map_path, str) or not map_path:
        raise SystemExit("audit-cost-missing-obligation-map")
    obligations = _audit_cost_artifact(
        out_dir, {"path": map_path, "sha256": value.get("obligation_map_sha256")}, "obligation-map"
    )
    if (
        not isinstance(obligations, list)
        or not obligations
        or not all(
            isinstance(item, dict)
            and item.get("preserved") is True
            and all(
                isinstance(item.get(field), str) and item[field].strip()
                for field in ("baseline", "candidate", "evidence")
            )
            for item in obligations
        )
    ):
        raise SystemExit("audit-cost-obligation-map-incomplete")
    static_gates = value.get("static_gates")
    expected = {"package", "tests", "calibration", "contract-markers", "adversarial-review"}
    if not isinstance(static_gates, list) or len(static_gates) != len(expected):
        raise SystemExit("audit-cost-static-guards-required")
    seen: set[str] = set()
    for guard in static_gates:
        if (
            not isinstance(guard, dict)
            or not isinstance(guard.get("id"), str)
            or guard.get("id") not in expected
            or guard["id"] in seen
            or guard.get("status") != "pass"
        ):
            raise SystemExit("audit-cost-static-guard-failed")
        seen.add(guard["id"])
        proof = _audit_cost_artifact(out_dir, guard.get("evidence"), guard["id"])
        if (
            not isinstance(proof, dict)
            or proof.get("status") != "pass"
            or any(proof.get(field) != value[field] for field in ("baseline_sha256", "candidate_sha256"))
        ):
            raise SystemExit("audit-cost-static-guard-failed")
    behavioral = _audit_cost_artifact(out_dir, value.get("behavioral_comparison"), "behavioral")
    live = _audit_cost_artifact(out_dir, value.get("live_comparison"), "live")
    for comparison in (behavioral, live):
        if not isinstance(comparison, dict) or any(
            comparison.get(field) != value[field] for field in ("baseline_sha256", "candidate_sha256")
        ):
            raise SystemExit("audit-cost-comparison-snapshot-mismatch")
    critical = _audit_cost_number(behavioral.get("critical_regressions"), "critical-regressions")
    baseline_failures = _audit_cost_number(behavioral.get("baseline_failures"), "baseline-failures")
    candidate_failures = _audit_cost_number(behavioral.get("candidate_failures"), "candidate-failures")
    if critical != 0 or candidate_failures > baseline_failures:
        raise SystemExit("audit-cost-behavior-regression")
    tasks = behavioral.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise SystemExit("audit-cost-paired-tasks-required")
    seen_tasks: set[str] = set()
    for task in tasks:
        if (
            not isinstance(task, dict)
            or not isinstance(task.get("id"), str)
            or not task["id"].strip()
            or task["id"] in seen_tasks
        ):
            raise SystemExit("audit-cost-invalid-task-identity")
        seen_tasks.add(task["id"])
        # Aggregate successes must not conceal worse completion or more tool/check failures on a paired task.
        assessments = []
        for side in ("baseline", "candidate"):
            assessment = task.get(side)
            if (
                not isinstance(assessment, dict)
                or not isinstance(assessment.get("evidence"), list)
                or not assessment["evidence"]
                or not all(isinstance(item, str) and item.strip() for item in assessment["evidence"])
            ):
                raise SystemExit("audit-cost-task-evidence-required")
            values = {
                field: _audit_cost_number(assessment.get(field), f"task-{side}-{field}")
                for field in ("completion_quality", "tool_failures", "check_failures")
            }
            if values["completion_quality"] > 1:
                raise SystemExit("audit-cost-invalid-completion-quality")
            assessments.append(values)
        baseline, candidate = assessments
        if (
            candidate["completion_quality"] < baseline["completion_quality"]
            or candidate["tool_failures"] > baseline["tool_failures"]
            or candidate["check_failures"] > baseline["check_failures"]
        ):
            raise SystemExit("audit-cost-task-behavior-regression")
    identity = live.get("baseline_identity")
    if (
        not isinstance(identity, dict)
        or set(identity) != {"model", "effort", "task_contract_sha256", "prompt_sha256"}
        or not all(isinstance(item, str) and item.strip() for item in identity.values())
        or live.get("candidate_identity") != identity
    ):
        raise SystemExit("audit-cost-unmatched-live-identity")
    if live.get("source") != "provider-native":
        raise SystemExit("audit-cost-native-live-evidence-required")
    for field in ("task_contract_sha256", "prompt_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", identity[field]):
            raise SystemExit("audit-cost-invalid-live-identity-digest")
    baseline_cost = _audit_cost_number(live.get("baseline_cost"), "baseline-cost", positive=True)
    candidate_cost = _audit_cost_number(live.get("candidate_cost"), "candidate-cost", positive=True)
    minimum = _audit_cost_number(live.get("min_cost_reduction"), "minimum-reduction", positive=True)
    if minimum >= 1 or (baseline_cost - candidate_cost) / baseline_cost + 1e-12 < minimum:
        raise SystemExit("audit-cost-insufficient-reduction")
