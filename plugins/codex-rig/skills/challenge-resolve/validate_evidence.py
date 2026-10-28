#!/usr/bin/env python3
"""Validate reviewer provenance and frozen source evidence for one adversarial loop.

## Purpose

Bind every recorded adversarial-review round to the exact request, source snapshot, diff, review route, and reviewer
output behind its ledger findings. This closes the gap left by ``adversarial_loop.py``: it checks deterministic
convergence structure but cannot prove a claimed reviewer inspected the retained material or that parent triage retained
every reported finding.

## Scope

Read only ``loop-ledger.json`` with its ``loop-rounds.jsonl`` round log, ``loop-evidence.json``, retained source and
supporting snapshots, and completed review runs below one challenge-resolve run. The validator delegates route, role,
child-lineage, and output checks to Code Review's existing manifest-only validator; it creates no reviewer, modifies no
artifact, and makes no network call. It
supports native schema-five through schema-eight and local reviewer wave schema-four evidence. Both retain a reviewer
thread identifier and frozen context bytes. Historical loop evidence schema one retains its request-coverage limit;
current schema two binds task criteria, declared unchanged callers or consumers, continuation lineage,
machine-readable reviewer-stated coverage, and an explicit parent mapping for every reviewer signature. Loop evidence
schema three is unsupported. Older review schemas are rejected.

## Usage

Run ``python validate_evidence.py --out <loop-run>`` after the source collector has written ``loop-evidence.json``.
Before an isolated paid review, run ``--preflight-plan <plan> --request-evidence <loop-evidence.json>
--supporting-source <supporting.json>`` to check exact frozen context delivery without a model turn.
``--codex-home`` identifies the local rollout store and otherwise uses the observed ``CODEX_HOME`` or canonical
``~/.codex`` location. Nonempty review rounds require the host's active ``CODEX_THREAD_ID`` to match loop author; never
override it from candidate evidence. Import ``validate_loop_evidence`` when a parent owns argument parsing.

## Outputs

A successful CLI invocation exits zero without changing files. A contract failure exits one with one stable error code
on stderr, including mismatched current worktree source, unsupported review evidence, missing full context, conflicting
reviewer findings, or a ledger that omits reviewer findings or changes fields outside recorded parent triage.

## Failure

Any path escaping the owning run, changed bytes, malformed evidence, substituted reviewer, absent triggered pass, or
non-identical source/diff binding raises ``ValueError``. The parent must preserve the failed evidence and obtain a fresh
permitted review rather than repairing provenance declarations after the fact.

## Used by

The challenge-resolve skill's parent-owned closure gate invokes this module before it accepts a clean review loop;
focused artifact tests exercise valid native evidence and forged identity, report, source, and context mutations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

#: Directory containing this validator, used to locate the plugin root.
SKILL_DIRECTORY = Path(__file__).resolve().parent
#: Root of the Codex Rig plugin, from which role cards and the code-review validator are read.
PLUGIN_ROOT = SKILL_DIRECTORY.parents[1]
#: Plugin shared directory placed on the import path for the adversarial-loop and diff helpers.
SHARED_DIRECTORY = PLUGIN_ROOT / "shared"
if str(SHARED_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SHARED_DIRECTORY))

from adversarial_loop import ledger_digest, load_ledger, summarize_ledger, validate_ledger  # noqa: E402
from collect_diff import canonical_diff_arguments, canonical_diff_environment, capture_source_snapshot  # noqa: E402

#: Pattern for a lowercase 64-character hexadecimal SHA-256 digest.
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
#: Exact key set required in each finding record of an evidence report.
_FINDING_KEYS = {"signature", "tier", "structural", "disposition", "evidence"}
#: Exact top-level key set of schema version 1 evidence.
_EVIDENCE_KEYS = {"schema_version", "repository", "scope_paths", "current_source_path", "rounds"}
#: Exact key set of each review round entry in schema version 1 evidence.
_ROUND_EVIDENCE_KEYS = {"index", "source_path", "review_run", "role"}
#: Exact top-level key set of schema version 2 evidence bound to a request and supporting sources.
_BOUND_EVIDENCE_KEYS = _EVIDENCE_KEYS | {"request", "supporting_paths", "current_supporting_source_path"}
#: Top-level key set of schema version 2 evidence that continues earlier work, adding an origin.
_CONTINUED_EVIDENCE_KEYS = _BOUND_EVIDENCE_KEYS | {"origin"}
#: Schema-3 binds the interactions assigned to the actual reviewer request and response.
_SCOPED_EVIDENCE_KEYS = _CONTINUED_EVIDENCE_KEYS | {"interaction_requirements"}
#: Round entry keys for bound evidence: the base round keys plus the supporting source path.
_BOUND_ROUND_KEYS = _ROUND_EVIDENCE_KEYS | {"supporting_source_path"}
#: Exact round entry key set for schema version 2 evidence, adding the triage record.
_TRIAGED_ROUND_KEYS = _BOUND_ROUND_KEYS | {"triage"}
#: Exact key set of each triage entry that re-maps a reported finding signature.
_TRIAGE_KEYS = {"reported_signature", "signature", "tier", "reason", "evidence"}
#: Exact key set of the request block describing the goal, specification, and completion criteria.
_REQUEST_KEYS = {"goal", "specification", "done_when"}
#: Pattern matching a whole report that is one adversarial-loop fenced block, with optional provenance comment.
_REPORT_BLOCK = re.compile(
    r"(?:<!-- codex-review-provenance role=[a-z-]+ run=\S+ input=[0-9a-f]{64} "
    r"context=[0-9a-f]{64} attempt=[1-9][0-9]* -->\n)?```adversarial-loop\n(.*?)\n```",
    re.DOTALL,
)


def _canonical_source_bytes(snapshot: dict[str, Any]) -> bytes:
    """Serialize source evidence with the collector's immutable portable representation."""
    return (json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def _sha256(content: bytes) -> str:
    """Return one lowercase SHA-256 digest for retained opaque artifact bytes."""
    return hashlib.sha256(content).hexdigest()


def _json_object(path: Path, error: str) -> dict[str, Any]:
    """Load exactly one UTF-8 JSON object or raise the supplied stable contract error."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exception:
        raise ValueError(error) from exception
    if not isinstance(value, dict):
        raise ValueError(error)
    return value


def _run_path(run_dir: Path, raw: object, error: str) -> Path:
    """Resolve one required relative evidence path while preventing run-directory escapes."""
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise ValueError(error)
    resolved = (run_dir / raw).resolve()
    try:
        resolved.relative_to(run_dir)
    except ValueError as exception:
        raise ValueError(error) from exception
    return resolved


def _review_path(review_run: Path, raw: object, error: str) -> Path:
    """Resolve a retained review path while allowing Code Review's contained absolute form."""
    if not isinstance(raw, str | Path) or not str(raw):
        raise ValueError(error)
    path = Path(raw)
    resolved = path.resolve() if path.is_absolute() else (review_run / path).resolve()
    try:
        resolved.relative_to(review_run)
    except ValueError as exception:
        raise ValueError(error) from exception
    return resolved


def _require_sha256(value: object, error: str) -> str:
    """Return a strict digest field or reject malformed digest declarations."""
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(error)
    return value


def _load_source_snapshot(
    path: Path, expected_repository: str, expected_scope: list[str]
) -> tuple[dict[str, Any], bytes]:
    """Load one retained source capture and bind its canonical bytes and declared coordinates."""
    snapshot = _json_object(path, "loop-evidence-source-invalid")
    bytes_value = path.read_bytes()
    if bytes_value != _canonical_source_bytes(snapshot):
        raise ValueError("loop-evidence-source-noncanonical")
    if snapshot.get("repository") != expected_repository or snapshot.get("scope_paths") != expected_scope:
        raise ValueError("loop-evidence-source-coordinate-mismatch")
    if not isinstance(snapshot.get("revision"), str) or not snapshot["revision"]:
        raise ValueError("loop-evidence-source-revision-invalid")
    return snapshot, bytes_value


def _require_supporting_source_records(snapshot: dict[str, Any], paths: list[str]) -> None:
    """Require every supporting path to have a captured file or deletion record."""
    records = snapshot.get("files")
    if not records:
        raise ValueError("loop-evidence-supporting-source-empty")
    if not isinstance(records, list) or any(
        not isinstance(record, dict)
        or not isinstance(record.get("path"), str)
        or not isinstance(record.get("kind"), str)
        or record.get("kind") not in {"file", "symlink", "missing"}
        for record in records
    ):
        raise ValueError("loop-evidence-supporting-source-incomplete")
    recorded_paths = {record["path"] for record in records}
    if not set(paths).issubset(recorded_paths):
        raise ValueError("loop-evidence-supporting-source-incomplete")


def _primary_source_paths(snapshot: dict[str, Any], scopes: list[str]) -> list[str]:
    """Require concrete captured records for every requested primary scope."""
    records = snapshot.get("files")
    if not isinstance(records, list) or any(
        not isinstance(record, dict)
        or not isinstance(record.get("path"), str)
        or not isinstance(record.get("kind"), str)
        or record.get("kind") not in {"file", "symlink", "missing"}
        for record in records
    ):
        raise ValueError("loop-evidence-primary-source-incomplete")
    paths = [record["path"] for record in records]
    if any(
        not any(scope == "." or path == scope or path.startswith(f"{scope}/") for path in paths) for scope in scopes
    ):
        raise ValueError("loop-evidence-primary-source-incomplete")
    return paths


def _interaction_requirements(value: object, request: dict[str, str]) -> list[dict[str, Any]]:
    """Validate typed interaction assignments and their exact frozen request binding."""
    if not isinstance(value, list):
        raise ValueError("loop-evidence-interaction-requirements-invalid")
    identities = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("loop-evidence-interaction-requirements-invalid")
        identity = item.get("id")
        is_audit = identity == "interaction-inventory-audit"
        expected = {"id", "source_paths", "claim"} | ({"unit_ids"} if is_audit else set())
        paths = item.get("source_paths")
        if (
            set(item) != expected
            or not isinstance(identity, str)
            or not identity.strip()
            or not isinstance(item.get("claim"), str)
            or not item["claim"].strip()
            or not isinstance(paths, list)
            or any(not isinstance(path, str) or not path for path in paths)
            or paths != sorted(set(paths))
            or (not is_audit and not paths)
        ):
            raise ValueError("loop-evidence-interaction-requirements-invalid")
        if is_audit:
            units = item["unit_ids"]
            if (
                not isinstance(units, list)
                or not units
                or any(not isinstance(unit, str) or not unit.strip() for unit in units)
                or units != sorted(set(units))
            ):
                raise ValueError("loop-evidence-interaction-requirements-invalid")
        identities.append(identity)
    if identities != sorted(set(identities)):
        raise ValueError("loop-evidence-interaction-requirements-invalid")
    block = "Interaction assessment:\n" + json.dumps(value, sort_keys=True, ensure_ascii=True)
    if not request["specification"].endswith("\n" + block):
        raise ValueError("loop-evidence-interaction-request-mismatch")
    return value


def _interaction_verdicts(value: object, requirements: list[dict[str, Any]], *, require_clean: bool) -> None:
    """Bind each assigned interaction and inventory unit to the reviewer's own verdict."""
    if not isinstance(value, list) or len(value) != len(requirements):
        raise ValueError("loop-evidence-interaction-verdict-invalid")
    for item, requirement in zip(value, requirements, strict=True):
        if not isinstance(item, dict):
            raise ValueError("loop-evidence-interaction-verdict-invalid")
        is_audit = requirement["id"] == "interaction-inventory-audit"
        keys = {"id", "verdict", "evidence"} | ({"unit_verdicts"} if is_audit else set())
        if (
            set(item) != keys
            or item["id"] != requirement["id"]
            or not isinstance(item["verdict"], str)
            or item["verdict"] not in {"reviewed", "unresolved"}
            or (require_clean and item["verdict"] != "reviewed")
            or not isinstance(item["evidence"], list)
            or not item["evidence"]
            or any(not isinstance(entry, str) or not entry.strip() for entry in item["evidence"])
        ):
            raise ValueError("loop-evidence-interaction-verdict-invalid")
        if is_audit:
            units = item["unit_verdicts"]
            if not isinstance(units, list) or len(units) != len(requirement["unit_ids"]):
                raise ValueError("loop-evidence-interaction-audit-invalid")
            for unit, identity in zip(units, requirement["unit_ids"], strict=True):
                if (
                    not isinstance(unit, dict)
                    or set(unit) != {"id", "verdict"}
                    or unit["id"] != identity
                    or not isinstance(unit["verdict"], str)
                    or unit["verdict"] not in {"reviewed", "unresolved"}
                    or (require_clean and unit["verdict"] != "reviewed")
                ):
                    raise ValueError("loop-evidence-interaction-audit-invalid")


def _comparison_base(evidence: dict[str, Any], repository: Path) -> str:
    """Return a validated immutable comparison base, or the legacy HEAD baseline."""
    base = evidence.get("diff_base_oid")
    if base is None:
        return "HEAD"
    if (
        evidence.get("schema_version") != 3
        or not isinstance(base, str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base) is None
    ):
        raise ValueError("loop-evidence-diff-base-invalid")
    try:
        resolved = (
            subprocess.run(  # noqa: S603 - argv list, no shell
                ["git", "-C", os.fspath(repository), "rev-parse", "--verify", base + "^{commit}"],  # noqa: S607 - tool resolved via PATH on purpose
                capture_output=True,
                check=True,
            )
            .stdout.decode("ascii")
            .strip()
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        raise ValueError("loop-evidence-diff-base-invalid") from exc
    if resolved != base:
        raise ValueError("loop-evidence-diff-base-invalid")
    return base


def _current_scoped_diff(
    repository_path: Path, scopes: list[str], expected_source: dict[str, Any], capture_error: str, base: str = "HEAD"
) -> bytes:
    """Read the current tracked patch and reject a concurrent scoped source change."""
    try:
        diff = subprocess.run(  # noqa: S603 - argv list, no shell
            [  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
                "git",
                "-C",
                os.fspath(repository_path),
                "--literal-pathspecs",
                *canonical_diff_arguments(("diff", base, "--binary", "--no-renames", "--", *scopes)),
            ],
            capture_output=True,
            check=True,
            env=canonical_diff_environment(),
        ).stdout
        if capture_source_snapshot(repository_path, scopes) != expected_source:
            raise ValueError("loop-evidence-current-source-mismatch")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exception:
        raise ValueError(capture_error) from exception
    return diff


def _check_context_material(
    context: bytes, source_bytes: bytes, diff_bytes: bytes, request_text: str | None, supporting_bytes: bytes | None
) -> None:
    """Check exact source, diff, request and supporting labels in one reviewer context."""
    try:
        context_text = context.decode("utf-8")
        source_text = source_bytes.decode("utf-8")
        diff_text = diff_bytes.decode("utf-8")
        supporting_text = supporting_bytes.decode("utf-8") if supporting_bytes is not None else None
    except UnicodeDecodeError as exception:
        raise ValueError("loop-evidence-review-context-material-not-utf8") from exception
    if (
        len(re.findall(r"(?m)^Frozen source:\n", context_text)) != 1
        or len(re.findall(r"(?m)^Frozen diff:\n", context_text)) != 1
    ):
        raise ValueError("loop-evidence-review-context-incomplete")
    supporting_labels = len(re.findall(r"(?m)^Supporting source:\n", context_text))
    if supporting_bytes is None and supporting_labels:
        raise ValueError("loop-evidence-review-context-incomplete")
    if supporting_bytes is not None and supporting_labels != 1:
        raise ValueError("loop-evidence-review-supporting-source-incomplete")
    if request_text is not None and len(re.findall(r"(?m)^Review request:\n", context_text)) != 1:
        raise ValueError("loop-evidence-review-request-incomplete")

    def matches_section(label: str, material: str, next_labels: tuple[str, ...]) -> bool:
        """Compare literal material at its anchored label without compiling it as a regex."""
        match = re.search(r"(?m)^" + label + "\n", context_text)
        if match is None or not context_text.startswith(material, match.end()):
            return False
        end = match.end() + len(material)
        if not context_text.startswith("\n", end):
            return False
        end += 1
        return any(
            context_text.startswith(next_label, end) if next_label else end == len(context_text)
            for next_label in next_labels
        )

    # Each material section ends at the next declared label (or context end).
    # Literal comparisons keep megabyte contexts out of the regex compiler.
    if not matches_section("Frozen source:", source_text, ("Frozen diff:\n",)):
        raise ValueError("loop-evidence-review-context-incomplete")
    if request_text is not None and not matches_section("Review request:", request_text, ("Frozen source:\n",)):
        raise ValueError("loop-evidence-review-request-incomplete")
    if supporting_text is not None and not matches_section("Supporting source:", supporting_text, ("",)):
        raise ValueError("loop-evidence-review-supporting-source-incomplete")
    next_labels = ("Supporting source:\n", "") if supporting_bytes is not None else ("",)
    if not matches_section("Frozen diff:", diff_text, next_labels):
        raise ValueError("loop-evidence-review-context-incomplete")
    if request_text is not None:
        # New request-bound reviews allow only the role card before evidence. Schema-one
        # reviews retain their historical preface readability but cannot promote current work.
        preface = context_text.split("Review request:\n", maxsplit=1)[0]
        challenger_card = (PLUGIN_ROOT / "roles" / "challenger" / "ROLE.md").read_text(encoding="utf-8")
        if preface not in {"", challenger_card + "\n"}:
            raise ValueError("loop-evidence-review-preface-invalid")


def _prior_findings_for_origin(origin: object, run: Path) -> dict[str, dict[str, Any]]:
    """Validate current-run lineage and load any retained prior findings."""
    if not isinstance(origin, dict) or set(origin) != {"kind", "caller_run", "prior_ledger_sha256"}:
        raise ValueError("loop-evidence-origin-invalid")
    if origin["kind"] == "first-run":
        if origin["caller_run"] is not None or origin["prior_ledger_sha256"] is not None:
            raise ValueError("loop-evidence-origin-invalid")
        return {}
    if origin["kind"] != "continuation":
        raise ValueError("loop-evidence-origin-invalid")
    caller = origin["caller_run"]
    prior_digest = origin["prior_ledger_sha256"]
    if (
        not isinstance(caller, str)
        or not caller
        or not isinstance(prior_digest, str)
        or not _SHA256.fullmatch(prior_digest)
    ):
        raise ValueError("loop-evidence-origin-invalid")
    caller_path = Path(caller)
    if not caller_path.is_absolute() or caller_path.resolve() == run or caller_path.resolve().as_posix() != caller:
        raise ValueError("loop-evidence-origin-invalid")
    try:
        caller_digest = ledger_digest(caller_path / "loop-ledger.json")
        prior_ledger = load_ledger(caller_path / "loop-ledger.json")
    except (OSError, ValueError) as exception:
        raise ValueError("loop-evidence-prior-ledger-invalid") from exception
    if (
        caller_digest != prior_digest
        or not isinstance(prior_ledger, dict)
        or validate_ledger(prior_ledger)
        or not prior_ledger["rounds"]
    ):
        raise ValueError("loop-evidence-prior-ledger-invalid")
    return {item["signature"]: item for item in prior_ledger["rounds"][-1]["findings"]}


def _require_prior_request(
    prior_findings: dict[str, dict[str, Any]],
    request: dict[str, str],
    interaction_requirements: list[dict[str, Any]] | None = None,
) -> None:
    """Require a continuation request to end with every retained final-round finding."""
    prior_block = "Prior findings to reassess:\n" + json.dumps(
        sorted(prior_findings.values(), key=lambda item: item["signature"]), sort_keys=True, ensure_ascii=True
    )
    specification = request["specification"]
    if interaction_requirements is not None:
        suffix = "\nInteraction assessment:\n" + json.dumps(interaction_requirements, sort_keys=True, ensure_ascii=True)
        if not specification.endswith(suffix):
            raise ValueError("loop-evidence-interaction-request-mismatch")
        specification = specification[: -len(suffix)]
    if not specification.endswith("\n" + prior_block):
        raise ValueError("loop-evidence-prior-request-incomplete")


def native_response_contract(evidence_path: Path, diff_bytes: bytes) -> dict[str, Any]:
    """Bind a native scoped response to explicit request and snapshot bytes."""
    evidence = _json_object(evidence_path, "loop-evidence-preflight-request-invalid")
    request = evidence.get("request")
    if (
        type(evidence.get("schema_version")) is not int
        or evidence["schema_version"] != 3
        or not isinstance(request, dict)
        or set(request) != _REQUEST_KEYS
        or any(not isinstance(value, str) or not value.strip() for value in request.values())
    ):
        raise ValueError("loop-evidence-preflight-request-invalid")
    requirements = _interaction_requirements(evidence.get("interaction_requirements"), request)
    base = evidence_path.resolve().parent
    source = _run_path(
        base, evidence.get("current_source_path"), "loop-evidence-preflight-source-path-invalid"
    ).read_bytes()
    support_path = evidence.get("current_supporting_source_path")
    support = (
        _run_path(base, support_path, "loop-evidence-preflight-supporting-path-invalid").read_bytes()
        if support_path is not None
        else None
    )
    return {
        "schema_version": 1,
        "evidence_schema_version": 3,
        "request_sha256": _sha256(json.dumps(request, sort_keys=True, ensure_ascii=True).encode("utf-8")),
        "source_sha256": _sha256(source),
        "supporting_source_sha256": _sha256(support) if support is not None else None,
        "diff_sha256": _sha256(diff_bytes),
        "interaction_requirements": requirements,
    }


def preflight_review_plan(plan_path: Path, request_evidence_path: Path, supporting_path: Path | None) -> None:
    """Reject a frozen challenger plan whose exact context would fail the later challenge evidence gate."""
    plan_dir = plan_path.resolve().parent
    plan = _json_object(plan_path, "loop-evidence-preflight-plan-invalid")
    evidence = _json_object(request_evidence_path, "loop-evidence-preflight-request-invalid")
    repository = evidence.get("repository")
    scopes = evidence.get("scope_paths")
    supporting_scopes = evidence.get("supporting_paths")
    request = evidence.get("request")
    if (
        type(evidence.get("schema_version")) is not int
        or evidence["schema_version"] not in {2, 3}
        or not isinstance(repository, str)
        or not isinstance(scopes, list)
        or not all(isinstance(path, str) for path in scopes)
        or not isinstance(supporting_scopes, list)
        or not all(isinstance(path, str) for path in supporting_scopes)
        or not isinstance(request, dict)
        or set(request) != _REQUEST_KEYS
        or any(not isinstance(value, str) or not value.strip() for value in request.values())
    ):
        raise ValueError("loop-evidence-preflight-request-invalid")
    base = _comparison_base(evidence, Path(repository))
    interaction_requirements = (
        _interaction_requirements(evidence.get("interaction_requirements"), request)
        if evidence["schema_version"] == 3
        else None
    )
    prior_findings = _prior_findings_for_origin(evidence.get("origin"), request_evidence_path.resolve().parent)
    if evidence["origin"]["kind"] == "continuation":
        _require_prior_request(prior_findings, request, interaction_requirements)
    native = "contexts" in plan
    evidence_dir = request_evidence_path.resolve().parent
    current_source_path = _run_path(
        evidence_dir, evidence.get("current_source_path"), "loop-evidence-preflight-source-path-mismatch"
    )
    source_path = (
        current_source_path
        if native
        else _run_path(plan_dir, plan.get("source_path"), "loop-evidence-preflight-source-path-invalid")
    )
    source, source_bytes = _load_source_snapshot(source_path, repository, scopes)
    try:
        _, evidence_source_bytes = _load_source_snapshot(current_source_path, repository, scopes)
    except ValueError as exception:
        raise ValueError("loop-evidence-preflight-source-path-mismatch") from exception
    if source_bytes != evidence_source_bytes:
        raise ValueError("loop-evidence-preflight-source-path-mismatch")
    if not native and _sha256(source_bytes) != plan.get("source_sha256"):
        raise ValueError("loop-evidence-preflight-source-digest-mismatch")
    diff_path = (
        plan_dir / "diff.patch"
        if native
        else _run_path(plan_dir, plan.get("diff_path"), "loop-evidence-preflight-diff-path-invalid")
    )
    try:
        diff_bytes = diff_path.read_bytes()
    except OSError as exception:
        raise ValueError("loop-evidence-preflight-diff-unreadable") from exception
    if not native and (
        _sha256(diff_bytes) != plan.get("diff_sha256")
        or plan.get("review_input_sha256", plan.get("diff_sha256")) != plan.get("diff_sha256")
    ):
        raise ValueError("loop-evidence-preflight-diff-digest-mismatch")
    repository_path = Path(repository)
    try:
        current_source = capture_source_snapshot(repository_path, scopes)
    except (OSError, RuntimeError, ValueError) as exception:
        raise ValueError("loop-evidence-current-source-capture-failed") from exception
    if source != current_source:
        raise ValueError("loop-evidence-current-source-mismatch")
    delivered_paths = set(_primary_source_paths(source, scopes))
    current_diff = _current_scoped_diff(
        repository_path, scopes, current_source, "loop-evidence-preflight-diff-capture-failed", base
    )
    if diff_bytes != current_diff:
        raise ValueError("loop-evidence-preflight-diff-current-mismatch")
    supporting_bytes: bytes | None = None
    if supporting_scopes:
        if supporting_path is None:
            raise ValueError("loop-evidence-preflight-supporting-missing")
        current_supporting_path = _run_path(
            evidence_dir,
            evidence.get("current_supporting_source_path"),
            "loop-evidence-preflight-supporting-path-mismatch",
        )
        supporting, supporting_bytes = _load_source_snapshot(supporting_path, repository, supporting_scopes)
        try:
            _, evidence_supporting_bytes = _load_source_snapshot(current_supporting_path, repository, supporting_scopes)
        except ValueError as exception:
            raise ValueError("loop-evidence-preflight-supporting-path-mismatch") from exception
        if supporting_bytes != evidence_supporting_bytes:
            raise ValueError("loop-evidence-preflight-supporting-path-mismatch")
        _require_supporting_source_records(supporting, supporting_scopes)
        delivered_paths.update(record["path"] for record in supporting["files"])
        if supporting.get("revision") != source.get("revision"):
            raise ValueError("loop-evidence-supporting-revision-mismatch")
        try:
            current_supporting = capture_source_snapshot(repository_path, supporting_scopes)
        except (OSError, RuntimeError, ValueError) as exception:
            raise ValueError("loop-evidence-current-supporting-capture-failed") from exception
        if supporting != current_supporting:
            raise ValueError("loop-evidence-current-supporting-mismatch")
    elif supporting_path is not None or evidence.get("current_supporting_source_path") is not None:
        raise ValueError("loop-evidence-preflight-supporting-unexpected")
    if interaction_requirements is not None and any(
        not set(item["source_paths"]).issubset(delivered_paths) for item in interaction_requirements
    ):
        raise ValueError("loop-evidence-interaction-source-missing")
    if native:
        if (
            plan.get("consumer_policy", {}).get("consumer_id") != "challenge-resolve"
            or plan.get("challenge_response") != native_response_contract(request_evidence_path, diff_bytes)
            or plan.get("review_input_sha256") != _sha256(diff_bytes)
        ):
            raise ValueError("loop-evidence-preflight-native-binding-invalid")
    nodes = plan.get("contexts") if native else plan.get("nodes")
    if (
        not isinstance(nodes, list)
        or len(nodes) != 1
        or not isinstance(nodes[0], dict)
        or nodes[0].get("role_id") != "challenger"
    ):
        raise ValueError("loop-evidence-preflight-nodes-invalid")
    node = nodes[0]
    context_path = _run_path(plan_dir, node.get("context_path"), "loop-evidence-preflight-context-path-invalid")
    try:
        context_bytes = context_path.read_bytes()
    except OSError as exception:
        raise ValueError("loop-evidence-review-context-unreadable") from exception
    if _sha256(context_bytes) != node.get("context_sha256"):
        raise ValueError("loop-evidence-preflight-context-digest-mismatch")
    _check_context_material(
        context_bytes,
        source_bytes,
        diff_bytes,
        json.dumps(request, sort_keys=True, ensure_ascii=True),
        supporting_bytes,
    )
    if native:
        # The native recipe must be the deterministic dispatch issued for this bound plan.
        code_review = PLUGIN_ROOT / "skills" / "code-review"
        if str(code_review) not in sys.path:
            sys.path.insert(0, str(code_review))
        import review_prepare

        dispatch = _json_object(plan_dir / "dispatch.json", "loop-evidence-preflight-dispatch-invalid")
        if dispatch.get("plan_sha256") != _sha256(plan_path.read_bytes()):
            raise ValueError("loop-evidence-preflight-dispatch-invalid")
        try:
            review_prepare._retained_reader_identity(plan_dir, dispatch)
        except (ValueError, SystemExit) as exception:
            raise ValueError("loop-evidence-preflight-dispatch-invalid") from exception


def _validate_manifest_only(review_run: Path, codex_home: Path, parent_thread_id: str) -> dict[str, Any]:
    """Delegate route and output provenance verification to Code Review's authoritative validator."""
    validator = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        [
            sys.executable,
            str(validator),
            "--out",
            str(review_run),
            "--manifest-only",
            "--challenge-only",
            "--codex-home",
            str(codex_home),
            "--parent-thread-id",
            parent_thread_id,
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        suffix = detail[-1] if detail else "unknown"
        raise ValueError(f"loop-evidence-review-manifest-invalid:{suffix}")
    return _json_object(review_run / "specialist-manifest.json", "loop-evidence-review-manifest-invalid")


def _findings_from_report(
    output: Path,
    source_digest: str,
    diff_digest: str,
    coverage_paths: list[str] | None = None,
    binary_paths: list[str] | None = None,
    interaction_requirements: list[dict[str, Any]] | None = None,
    *,
    require_clean: bool = True,
) -> list[dict[str, Any]]:
    """Require complete frozen-source coverage without discarding additional inspected evidence.

    Extra path declarations remain reviewer claims; they never extend the independently bound frozen source scope.
    """
    try:
        text = output.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exception:
        raise ValueError("loop-evidence-report-unreadable") from exception
    block = _REPORT_BLOCK.fullmatch(text.strip())
    if block is None and not text.lstrip().startswith("{"):
        raise ValueError("loop-evidence-report-envelope-invalid")
    if block is not None and "\n```" in block.group(1):
        raise ValueError("loop-evidence-report-envelope-invalid")
    try:
        payload = json.loads(block.group(1) if block is not None else text)
    except json.JSONDecodeError as exception:
        raise ValueError("loop-evidence-report-block-invalid") from exception
    base_keys = {"source_sha256", "diff_sha256", "findings"}
    expected_keys = (
        (base_keys | {"assessment", "interactions"},)
        if interaction_requirements is not None
        else (
            base_keys,
            base_keys | {"assessment"},
        )
    )
    if not isinstance(payload, dict) or set(payload) not in expected_keys:
        raise ValueError("loop-evidence-report-block-shape-invalid")
    if interaction_requirements is not None:
        _interaction_verdicts(payload["interactions"], interaction_requirements, require_clean=require_clean)
    if "assessment" in payload:
        assessment = payload["assessment"]
        legacy_keys = {"rating", "rationale"}
        current_keys = legacy_keys | {"source_rating_version", "verification_readiness"}
        if (
            not isinstance(assessment, dict)
            or set(assessment) not in (legacy_keys, current_keys)
            or (
                set(assessment) == current_keys
                and (
                    type(assessment["source_rating_version"]) is not int
                    or assessment["source_rating_version"] != 1
                    or assessment["verification_readiness"] != "complete"
                )
            )
            or type(assessment["rating"]) is not int
            or assessment["rating"] not in range(1, 6)
            or not isinstance(assessment["rationale"], str)
            or not assessment["rationale"].strip()
        ):
            raise ValueError("loop-evidence-report-assessment-invalid")
    if coverage_paths is not None:
        assessment = payload.get("assessment")
        rationale = assessment.get("rationale", "") if isinstance(assessment, dict) else ""
        try:
            coverage = json.loads(rationale)
        except json.JSONDecodeError as exception:
            raise ValueError("loop-evidence-report-coverage-missing") from exception
        if (
            not isinstance(coverage, dict)
            or set(coverage) != {"reviewed_paths", "unreviewed_paths", "limits", "judgment"}
            or not isinstance(coverage["reviewed_paths"], list)
            or any(not isinstance(path, str) or not path for path in coverage["reviewed_paths"])
            or coverage["reviewed_paths"] != sorted(set(coverage["reviewed_paths"]))
            or not set(coverage_paths).issubset(coverage["reviewed_paths"])
            or coverage["unreviewed_paths"] != []
            or not isinstance(coverage["limits"], str)
            or not coverage["limits"].strip()
            or not isinstance(coverage["judgment"], str)
            or not coverage["judgment"].strip()
        ):
            raise ValueError("loop-evidence-report-coverage-missing")
        if binary_paths and (
            "binary" not in coverage["limits"].casefold()
            or any(path not in coverage["limits"] for path in binary_paths)
        ):
            raise ValueError("loop-evidence-binary-source-limit-missing")
    if payload["source_sha256"] != source_digest or payload["diff_sha256"] != diff_digest:
        raise ValueError("loop-evidence-report-block-digest-mismatch")
    findings = payload["findings"]
    if not isinstance(findings, list) or any(
        not isinstance(item, dict) or set(item) != _FINDING_KEYS for item in findings
    ):
        raise ValueError("loop-evidence-report-findings-invalid")
    if coverage_paths is not None and not findings and payload["assessment"]["rating"] != 1:
        raise ValueError("loop-evidence-report-clean-rating-mismatch")
    return findings


def _selected_native_pass(
    manifest: dict[str, Any], review_run: Path, role: str, *, raw: bool = False
) -> tuple[Path, str, Path]:
    """Return the selected native output, reviewer thread, and frozen context for one exact role."""
    if manifest.get("schema_version") not in {5, 6, 7, 8}:
        raise ValueError("loop-evidence-review-schema-unsupported")
    passes = manifest.get("passes")
    if not isinstance(passes, list):
        raise ValueError("loop-evidence-review-passes-invalid")
    by_role = {item.get("role"): item for item in passes if isinstance(item, dict)}
    selected = by_role.get(role)
    if len(by_role) != len(passes) or not isinstance(selected, dict) or selected.get("mode") != "inspection":
        raise ValueError("loop-evidence-review-role-invalid")
    attempts = selected.get("attempts")
    position = selected.get("selected_attempt")
    if not isinstance(attempts, list) or not isinstance(position, int) or not 1 <= position <= len(attempts):
        raise ValueError("loop-evidence-review-selected-attempt-invalid")
    attempt = attempts[position - 1]
    if not isinstance(attempt, dict) or not isinstance(attempt.get("agent_thread_id"), str):
        raise ValueError("loop-evidence-review-selected-attempt-invalid")
    return (
        _run_path(
            review_run,
            attempt.get("raw_output_path" if raw else "output_path"),
            "loop-evidence-review-output-path-invalid",
        ),
        attempt["agent_thread_id"],
        _run_path(review_run, attempt.get("context_path"), "loop-evidence-review-context-path-invalid"),
    )


def _selected_local_reviewer_pass(manifest: dict[str, Any], review_run: Path, role: str) -> tuple[Path, str, Path]:
    """Return selected schema-four output, observed thread, and frozen context for one exact role."""
    if manifest.get("schema_version") != 4:
        raise ValueError("loop-evidence-review-schema-unsupported")
    execution = manifest.get("app_server_execution")
    if not isinstance(execution, dict):
        raise ValueError("loop-evidence-review-app-server-invalid")
    evidence_path = _review_path(review_run, execution.get("evidence_path"), "loop-evidence-review-app-server-invalid")
    evidence = _json_object(evidence_path, "loop-evidence-review-app-server-invalid")
    nodes = evidence.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("loop-evidence-review-app-server-invalid")
    matches = [node for node in nodes if isinstance(node, dict) and node.get("role_id") == role]
    if len(matches) != 1 or not isinstance(matches[0].get("thread_id"), str):
        raise ValueError("loop-evidence-review-role-invalid")
    node = matches[0]
    plan_path = _review_path(review_run, execution.get("plan_path"), "loop-evidence-review-app-server-invalid")
    return (
        _review_path(
            review_run,
            evidence_path.parent / str(node.get("output_path", "")),
            "loop-evidence-review-output-path-invalid",
        ),
        node["thread_id"],
        _review_path(
            review_run,
            plan_path.parent / str(node.get("context_path", "")),
            "loop-evidence-review-context-path-invalid",
        ),
    )


def _validate_review_round(
    review_run: Path,
    codex_home: Path,
    expected_input_digest: str,
    selected_role: str,
    author: str,
    ledger_reviewer_identity: object,
    source_bytes: bytes,
    diff_bytes: bytes,
    request_text: str | None,
    supporting_bytes: bytes | None,
    ledger_report: Path,
    ledger_findings: list[dict[str, Any]],
    triage: list[dict[str, Any]] | None,
    coverage_paths: list[str] | None = None,
    required_prior_signatures: set[str] | None = None,
    binary_paths: list[str] | None = None,
    interaction_requirements: list[dict[str, Any]] | None = None,
    *,
    require_clean: bool = True,
) -> None:
    """Bind one frozen review to its ledger, applying explicit parent identity and severity triage when present."""
    preflight_manifest = _json_object(review_run / "specialist-manifest.json", "loop-evidence-review-manifest-invalid")
    parent_thread_id = preflight_manifest.get("parent_thread_id")
    if not isinstance(parent_thread_id, str) or not parent_thread_id:
        raise ValueError("loop-evidence-review-parent-invalid")
    if parent_thread_id != author:
        raise ValueError("loop-evidence-implementation-author-mismatch")
    manifest = _validate_manifest_only(review_run, codex_home, parent_thread_id)
    if manifest.get("review_input_sha256") != expected_input_digest:
        raise ValueError("loop-evidence-review-input-digest-mismatch")
    if (review_run / "diff.patch").read_bytes() != diff_bytes:
        raise ValueError("loop-evidence-review-diff-mismatch")
    schema = manifest.get("schema_version")
    if schema in {5, 6, 7, 8}:
        plan = _json_object(review_run / "inspection-plan.json", "loop-evidence-preflight-plan-invalid")
        expected_contract = None
        if interaction_requirements is not None:
            if request_text is None:
                raise ValueError("loop-evidence-preflight-request-invalid")
            expected_contract = {
                "schema_version": 1,
                "evidence_schema_version": 3,
                "request_sha256": _sha256(request_text.encode("utf-8")),
                "source_sha256": _sha256(source_bytes),
                "supporting_source_sha256": _sha256(supporting_bytes) if supporting_bytes is not None else None,
                "diff_sha256": _sha256(diff_bytes),
                "interaction_requirements": interaction_requirements,
            }
        if (schema in {7, 8} or "challenge_response" in plan) and plan.get("challenge_response") != expected_contract:
            raise ValueError("loop-evidence-review-native-binding-mismatch")
        selected_output, reviewer_identity, _ = _selected_native_pass(manifest, review_run, selected_role)
        selector = _selected_native_pass
    elif schema == 4:
        selected_output, reviewer_identity, _ = _selected_local_reviewer_pass(manifest, review_run, selected_role)
        selector = _selected_local_reviewer_pass
    else:
        raise ValueError("loop-evidence-review-schema-unsupported")
    if reviewer_identity == author:
        raise ValueError("loop-evidence-reviewer-is-implementation-author")
    if ledger_reviewer_identity != reviewer_identity:
        raise ValueError("loop-evidence-reviewer-identity-mismatch")
    accepted_outputs = [selected_output.read_bytes()]
    if schema in {7, 8}:
        raw_output, _, _ = _selected_native_pass(manifest, review_run, selected_role, raw=True)
        accepted_outputs.append(raw_output.read_bytes())
    if ledger_report.read_bytes() not in accepted_outputs:
        raise ValueError("loop-evidence-report-bytes-mismatch")

    passes = manifest.get("passes")
    if not isinstance(passes, list) or not passes:
        raise ValueError("loop-evidence-review-passes-invalid")
    merged: dict[str, dict[str, Any]] = {}
    for item in passes:
        if not isinstance(item, dict) or not isinstance(item.get("role"), str):
            raise ValueError("loop-evidence-review-passes-invalid")
        output, pass_identity, context = selector(manifest, review_run, item["role"])
        if pass_identity == author:
            raise ValueError("loop-evidence-reviewer-is-implementation-author")
        try:
            # Newline conversion would reject intact CRLF diffs and admit altered LF diffs.
            context_bytes = context.read_bytes()
        except OSError as exception:
            raise ValueError("loop-evidence-review-context-unreadable") from exception
        _check_context_material(context_bytes, source_bytes, diff_bytes, request_text, supporting_bytes)
        reported_signatures: set[str] = set()
        for finding in _findings_from_report(
            output,
            _sha256(source_bytes),
            _sha256(diff_bytes),
            coverage_paths,
            binary_paths,
            interaction_requirements,
            require_clean=require_clean,
        ):
            signature = finding["signature"]
            if not isinstance(signature, str) or not signature:
                raise ValueError("loop-evidence-report-findings-invalid")
            if triage is not None and signature in reported_signatures:
                raise ValueError("loop-evidence-triage-raw-duplicate")
            reported_signatures.add(signature)
            evidence = finding["evidence"]
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(not isinstance(entry, str) or not entry.strip() for entry in evidence)
            ):
                raise ValueError("loop-evidence-report-findings-invalid")
            prior = merged.get(signature)
            if prior is not None:
                if any(prior[key] != finding[key] for key in ("tier", "structural", "disposition")):
                    raise ValueError(f"loop-evidence-report-finding-conflict:{signature}")
                # Corroborating reviewers may supply different proof for the same verdict.
                # Preserve exact strings in first-seen order; never choose one reviewer's evidence.
                prior["evidence"] = list(dict.fromkeys([*prior["evidence"], *evidence]))
            else:
                merged[signature] = finding
    for signature in sorted((required_prior_signatures or set()) - merged.keys()):
        raise ValueError(f"loop-evidence-prior-raw-verdict-missing:{signature}")
    ledger_by_signature = {item.get("signature"): item for item in ledger_findings if isinstance(item, dict)}
    for signature in sorted(required_prior_signatures or set()):
        if signature not in ledger_by_signature or merged[signature]["tier"] != ledger_by_signature[signature].get(
            "tier"
        ):
            raise ValueError(f"loop-evidence-prior-raw-tier-mismatch:{signature}")
    if triage is not None:
        if not isinstance(triage, list):
            raise ValueError("loop-evidence-triage-invalid")
        mapped: dict[str, dict[str, Any]] = {}
        seen_raw: set[str] = set()
        for item in triage:
            if not isinstance(item, dict) or set(item) != _TRIAGE_KEYS:
                raise ValueError("loop-evidence-triage-invalid")
            reported_signature = item["reported_signature"]
            signature = item["signature"]
            tier = item["tier"]
            reason = item["reason"]
            proof = item["evidence"]
            if (
                not isinstance(reported_signature, str)
                or not reported_signature.strip()
                or not isinstance(signature, str)
                or not signature.strip()
                or not isinstance(tier, str)
                or not isinstance(reason, str)
                or not isinstance(proof, list)
                or any(not isinstance(value, str) or not value.strip() for value in proof)
            ):
                raise ValueError("loop-evidence-triage-invalid")
            if reported_signature in seen_raw:
                raise ValueError("loop-evidence-triage-raw-duplicate")
            seen_raw.add(reported_signature)
            if signature in mapped:
                raise ValueError("loop-evidence-triage-signature-duplicate")
            raw = merged.get(reported_signature)
            if raw is None:
                raise ValueError("loop-evidence-triage-coverage-invalid")
            if required_prior_signatures and signature != reported_signature:
                for prior_signature in sorted({signature, reported_signature} & required_prior_signatures):
                    raise ValueError(f"loop-evidence-prior-raw-verdict-missing:{prior_signature}")
            corrected = signature != reported_signature or tier != raw["tier"]
            if corrected and (not reason.strip() or not proof):
                raise ValueError("loop-evidence-triage-correction-proof-required")
            if not corrected and (reason or proof):
                raise ValueError("loop-evidence-triage-unnecessary-proof")
            mapped[signature] = {**raw, "signature": signature, "tier": tier}
        if seen_raw != merged.keys():
            raise ValueError("loop-evidence-triage-coverage-invalid")
        merged = mapped
    if len(ledger_by_signature) != len(ledger_findings) or merged != ledger_by_signature:
        raise ValueError("loop-evidence-report-findings-ledger-mismatch")


def validate_loop_evidence(run_dir: Path, codex_home: Path) -> None:
    """Raise ``ValueError`` unless retained loop evidence authenticates every recorded review round."""
    run = run_dir.resolve()
    evidence = _json_object(run / "loop-evidence.json", "loop-evidence-invalid")
    version = evidence.get("schema_version")
    if (
        (version == 1 and set(evidence) != _EVIDENCE_KEYS)
        or (version == 2 and set(evidence) not in (_BOUND_EVIDENCE_KEYS, _CONTINUED_EVIDENCE_KEYS))
        or (version == 3 and set(evidence) not in (_SCOPED_EVIDENCE_KEYS, _SCOPED_EVIDENCE_KEYS | {"diff_base_oid"}))
        or type(version) is not int
        or version not in (1, 2, 3)
    ):
        raise ValueError("loop-evidence-invalid")
    repository = evidence.get("repository")
    scopes = evidence.get("scope_paths")
    if (
        not isinstance(repository, str)
        or not isinstance(scopes, list)
        or not all(isinstance(item, str) for item in scopes)
    ):
        raise ValueError("loop-evidence-invalid")
    repository_path = Path(repository)
    base = _comparison_base(evidence, repository_path)
    if not repository_path.is_absolute() or repository_path.resolve().as_posix() != repository:
        raise ValueError("loop-evidence-repository-invalid")
    current_path = _run_path(run, evidence.get("current_source_path"), "loop-evidence-current-source-path-invalid")
    current, current_bytes = _load_source_snapshot(current_path, repository, scopes)
    try:
        actual = capture_source_snapshot(repository_path, scopes)
    except (OSError, RuntimeError, ValueError) as exception:
        raise ValueError("loop-evidence-current-source-capture-failed") from exception
    if current != actual:
        raise ValueError("loop-evidence-current-source-mismatch")
    if "origin" in evidence:
        _primary_source_paths(actual, scopes)
    request_text: str | None = None
    supporting_paths: list[str] = []
    current_supporting_bytes: bytes | None = None
    interaction_requirements: list[dict[str, Any]] | None = None
    if version in {2, 3}:
        request = evidence["request"]
        supporting_paths = evidence["supporting_paths"]
        if (
            not isinstance(request, dict)
            or set(request) != _REQUEST_KEYS
            or any(not isinstance(value, str) or not value.strip() for value in request.values())
            or not isinstance(supporting_paths, list)
            or any(not isinstance(path, str) or not path for path in supporting_paths)
            or len(set(supporting_paths)) != len(supporting_paths)
        ):
            raise ValueError("loop-evidence-request-invalid")
        if version == 3:
            interaction_requirements = _interaction_requirements(evidence["interaction_requirements"], request)
        request_text = json.dumps(request, sort_keys=True, ensure_ascii=True)
        if supporting_paths:
            path = _run_path(run, evidence["current_supporting_source_path"], "loop-evidence-supporting-path-invalid")
            supporting, current_supporting_bytes = _load_source_snapshot(path, repository, supporting_paths)
            _require_supporting_source_records(supporting, supporting_paths)
            try:
                actual_supporting = capture_source_snapshot(repository_path, supporting_paths)
            except (OSError, RuntimeError, ValueError) as exception:
                raise ValueError("loop-evidence-current-supporting-capture-failed") from exception
            if supporting != actual_supporting:
                raise ValueError("loop-evidence-current-supporting-mismatch")
        elif evidence["current_supporting_source_path"] is not None:
            raise ValueError("loop-evidence-supporting-path-invalid")

    try:
        ledger = load_ledger(run / "loop-ledger.json")
    except (OSError, ValueError) as exception:
        raise ValueError("loop-evidence-ledger-invalid") from exception
    if not isinstance(ledger, dict):
        raise ValueError("loop-evidence-ledger-invalid")
    errors = validate_ledger(ledger)
    if errors:
        raise ValueError("loop-evidence-ledger-invalid:" + ";".join(errors))
    summary = summarize_ledger(ledger)
    has_origin = "origin" in evidence
    if has_origin:
        prior_findings = _prior_findings_for_origin(evidence["origin"], run)
        if evidence["origin"]["kind"] == "continuation":
            # With no review there is no verdict to check; the bound prior findings stay open.
            if ledger["rounds"]:
                current_findings = {item["signature"]: item for item in ledger["rounds"][-1]["findings"]}
                for signature, prior in prior_findings.items():
                    current_finding = current_findings.get(signature)
                    if (
                        current_finding is None
                        or current_finding["tier"] != prior["tier"]
                        or current_finding["structural"] != prior["structural"]
                        or (
                            summary["reason"] == "clean"
                            and current_finding["disposition"] not in {"verified-fixed", "rejected"}
                        )
                    ):
                        raise ValueError(f"loop-evidence-prior-signature-unresolved:{signature}")
            _require_prior_request(prior_findings, evidence["request"], interaction_requirements)
    if current.get("revision") != ledger["current_snapshot"]["revision"]:
        raise ValueError("loop-evidence-current-source-revision-mismatch")
    rounds = ledger["rounds"]
    entries = evidence.get("rounds")
    if not isinstance(entries, list) or len(entries) != len(rounds):
        raise ValueError("loop-evidence-round-count-mismatch")
    author = ledger["implementation_author"]
    if not isinstance(author, str):
        raise TypeError(f"author must be str, got {type(author).__name__}")
    if rounds:
        active_owner = os.environ.get("CODEX_THREAD_ID", "")
        if not active_owner.strip():
            raise ValueError("loop-evidence-active-owner-missing")
        if active_owner != author:
            raise ValueError("loop-evidence-active-owner-mismatch")
    for ledger_round, entry in zip(rounds, entries, strict=True):
        if (
            not isinstance(entry, dict)
            or set(entry) != (_TRIAGED_ROUND_KEYS if version in {2, 3} else _ROUND_EVIDENCE_KEYS)
            or entry.get("index") != ledger_round["index"]
        ):
            raise ValueError("loop-evidence-round-shape-invalid")
        if entry.get("role") != "challenger":
            raise ValueError("loop-evidence-round-role-invalid")
        source_path = _run_path(run, entry.get("source_path"), "loop-evidence-source-path-invalid")
        source, source_bytes = _load_source_snapshot(source_path, repository, scopes)
        primary_paths = _primary_source_paths(source, scopes) if has_origin else []
        supporting_bytes: bytes | None = None
        binary_paths = [record["path"] for record in source["files"] if record.get("encoding") == "base64"]
        if supporting_paths:
            supporting_path = _run_path(run, entry["supporting_source_path"], "loop-evidence-supporting-path-invalid")
            supporting, supporting_bytes = _load_source_snapshot(supporting_path, repository, supporting_paths)
            _require_supporting_source_records(supporting, supporting_paths)
            binary_paths.extend(record["path"] for record in supporting["files"] if record.get("encoding") == "base64")
            if supporting["revision"] != source["revision"]:
                raise ValueError("loop-evidence-supporting-revision-mismatch")
            if (
                summary["reason"] == "clean"
                and ledger_round is rounds[-1]
                and supporting_bytes != current_supporting_bytes
            ):
                raise ValueError("loop-evidence-final-supporting-mismatch")
        elif version in {2, 3} and entry["supporting_source_path"] is not None:
            raise ValueError("loop-evidence-supporting-path-invalid")
        if interaction_requirements is not None:
            delivered_paths = set(primary_paths)
            if supporting_paths:
                delivered_paths.update(record["path"] for record in supporting["files"])
            if any(not set(item["source_paths"]).issubset(delivered_paths) for item in interaction_requirements):
                raise ValueError("loop-evidence-interaction-source-missing")
        if source.get("revision") != ledger_round["snapshot"]["revision"]:
            raise ValueError("loop-evidence-source-revision-mismatch")
        if summary["reason"] == "clean" and ledger_round is rounds[-1] and source_bytes != current_bytes:
            raise ValueError("loop-evidence-final-source-mismatch")
        diff_path = run / f"round-{ledger_round['index']}.diff"
        if not diff_path.is_file():
            raise ValueError("loop-evidence-round-diff-missing")
        diff_bytes = diff_path.read_bytes()
        digest = _require_sha256(ledger_round["snapshot"].get("diff_digest"), "loop-evidence-round-digest-invalid")
        if _sha256(diff_bytes) != digest:
            raise ValueError("loop-evidence-round-diff-digest-mismatch")
        review_run = _run_path(run, entry.get("review_run"), "loop-evidence-review-run-path-invalid")
        report = _run_path(run, ledger_round.get("report_path"), "loop-evidence-report-path-invalid")
        _validate_review_round(
            review_run,
            codex_home.resolve(),
            digest,
            entry["role"],
            author,
            ledger_round["reviewer"].get("identity"),
            source_bytes,
            diff_bytes,
            request_text,
            supporting_bytes,
            report,
            ledger_round["findings"],
            entry["triage"] if version in {2, 3} else None,
            sorted(set([*primary_paths, *supporting_paths])) if has_origin else None,
            set(prior_findings)
            if has_origin and evidence["origin"]["kind"] == "continuation" and ledger_round is rounds[-1]
            else None,
            sorted(set(binary_paths)) if has_origin else None,
            interaction_requirements,
            require_clean=summary["reason"] == "clean" and ledger_round is rounds[-1],
        )
        if version in {2, 3} and summary["reason"] == "clean" and ledger_round is rounds[-1]:
            current_diff = _current_scoped_diff(
                repository_path, scopes, actual, "loop-evidence-final-diff-capture-failed", base
            )
            if diff_bytes != current_diff:
                raise ValueError("loop-evidence-final-diff-current-mismatch")


def main() -> int:
    """Run the read-only loop-evidence validation command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--out", type=Path, help="Adversarial-loop run directory.")
    mode.add_argument(
        "--preflight-plan", type=Path, help="Frozen isolated reviewer plan, checked without a model turn."
    )
    parser.add_argument(
        "--request-evidence", type=Path, help="Bound loop-evidence JSON holding review request and scope."
    )
    parser.add_argument("--supporting-source", type=Path, help="Frozen supporting source JSON, when declared.")
    configured_home = os.environ.get("CODEX_HOME")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(configured_home) if configured_home else Path.home() / ".codex",
        help="Codex home containing rollout session logs.",
    )
    args = parser.parse_args()
    if args.preflight_plan is not None and args.request_evidence is None:
        parser.error("--preflight-plan requires --request-evidence")
    if args.out is not None and (args.request_evidence is not None or args.supporting_source is not None):
        parser.error("--request-evidence and --supporting-source require --preflight-plan")
    try:
        if args.preflight_plan is not None:
            preflight_review_plan(args.preflight_plan, args.request_evidence, args.supporting_source)
        else:
            validate_loop_evidence(args.out, args.codex_home)
    except ValueError as exception:
        print(str(exception), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
