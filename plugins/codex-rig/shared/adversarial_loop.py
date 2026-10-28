#!/usr/bin/env python3
"""Validate deterministic evidence for a bounded adversarial review loop.

## Purpose

Keep the review-and-fix convergence rule executable so a lifecycle owner cannot relabel self-review as independent, lose
an open finding, or continue after a terminal convergence decision. The helper computes scores from recorded findings
rather than trusting a supplied total.

## Scope

Validate one scoped ledger whose weighted score strictly decreases after the baseline review. Scores are nonnegative
integers, so continued improvement reaches a terminal result within a finite number of rounds without a fixed round
cap. It checks record shape and consistency, but does not establish that a named reviewer, revision, digest, or report
really exists; those fields are traceability evidence for an external owner to inspect.

The current schema 2 splits the ledger in two files so completed rounds are only ever appended. ``loop-ledger.json`` is
a small header (``schema_version``, ``implementation_author``, ``current_snapshot``) that the owner rewrites as the
current snapshot moves. The sibling ``loop-rounds.jsonl`` holds one round object per line in index order. A header
named ``<prefix>ledger.json`` pairs with ``<prefix>rounds.jsonl``, so a copied ``prior-loop-ledger.json`` keeps its own
round log. An absent round log means no completed round. Historical schema 1 kept every round inline in one rewritten
JSON file; those archives stay readable through the same loader, but a current result requires schema 2.

## Usage

Run ``python <dir>/adversarial_loop.py --ledger path/to/loop-ledger.json``, where ``<dir>`` is the directory that
ships this file: ``shared/`` in Codex Rig, ``bin/`` in each Claude plugin. Import ``load_ledger`` to assemble the
header and round log into the object ``validate_ledger`` checks, ``summarize_ledger`` for the inferred status, reason,
and open-finding scores, and ``ledger_digest`` for the identity a continuation records for a ledger on disk.

Record each completed round by writing it as one JSON object to ``loop-rounds.jsonl.rec`` beside the header, then
running the CLI with ``--append``: the staged round is validated against the earlier rounds, appended as one line, and
the staged file is deleted. Earlier rounds are never re-emitted, so a dropped or reworded round cannot slip in.

Record each round's cost beside it the same way: write one object to ``loop-metrics.jsonl.rec`` and add
``--append-metrics`` to the same call. The record has eleven fields: ``schema_version`` (2), ``kind`` (``round``), a
completed ``round``, the review ``route``, ``brief`` (``full`` or ``lean``), and ``brief_bytes``, ``duration_s``,
``tokens_in``, ``tokens_out``, ``tokens_total``, ``attested_gaps``, each ``null`` when a route cannot report it.
``tokens_total`` is the single token total a route reports without an input/output split, such as a Claude background
agent's ``subagent_tokens``. ``duration_s`` is a nonnegative number of seconds; the other five are nonnegative
integers, and ``attested_gaps`` is ``null`` after round 1 or when no attestation was written. Schema 1 records, which
carry no ``tokens_total``, stay accepted.
It is advisory data for later cost analysis and never feeds scoring, recurrence, or clean acceptance: a rejected record
only prints ``warning: metrics-rejected:<codes>`` and keeps its staged file, so the round, the progress table and the
exit status are unaffected. A second record for the same round supersedes the first (last record per round wins). Its
``route`` is ``parent`` or ``agent:``, ``bridge:``, ``codex:``, ``other:`` plus a label; any other shape only draws a
warning. ``--append`` likewise warns, never refuses, when an open finding first reported after round 1 has no
``origin:`` evidence entry naming ``earlier-snapshot``, ``resolver-edit``, ``scope-added`` or ``undated``.
``--plateau-kind`` prints ``stuck``, ``whack-a-mole``, ``mixed`` or ``none`` on stderr for a plateau or non-converging
latest round without touching stdout or the exit status. ``--brief-arm`` prints ``full`` or ``lean`` (stable per run
directory name, ``full`` while a ``brief-arm-pause`` file sits beside the run directory) for the reviewer-brief pilot
and exits; it cannot be combined with another action.

Use ``--require-clean`` when an acceptance gate must reject every valid non-clean outcome. Add ``--progress`` after each
completed round to print its cumulative severity table on stderr, leaving machine-readable stdout unchanged. Add
``--actions path/to/loop-actions.json`` after parent triage to bind every open finding to a resolution action. The
current actions schema 3 names an ``invariant:`` on every action, so recurrence is counted per signature or invariant;
schema-1 and schema-2 action records stay readable under their own evidence rules.

## Outputs

The CLI prints one JSON object. A valid ledger yields its deterministic summary and exit status zero; an invalid ledger
yields ``status=invalid``, error names, and exit status one. ``--require-clean`` preserves the valid JSON summary but
exits one unless its reason is ``clean``. Optional progress cells split old and new open findings, with security and
critical in separate columns and their distinct weights; no table is printed before a completed round. Action
validation checks record consistency, not whether a claimed fix or cause is true. Only ``--append`` and
``--append-metrics`` modify files: each adds one line to its log (round log, cost log) and removes its staged record; a
rejected round is kept for inspection and nothing is appended, and a rejected cost record is kept the same way.

## Failure

The contract blocks clean acceptance without current-snapshot independent coverage, stops plateau and nonconverging
loops, and rejects malformed records or history that loses a stable finding. Scope and authority checks for
structural or severe fixes remain with the parent workflow. A repeated fix without root-cause evidence is rejected by
the optional action validation.

## Used by

Review lifecycle owners and their tests use this small helper before deciding whether to fix, stop, or request a
separately scoped review run. It deliberately contains no resume override, dispatch mechanism, or provenance API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

#: Current schema revision of the loop ledger header; a ledger with any other version is rejected.
SCHEMA_VERSION = 2
#: Historical single-file schema with every round inline; readable archives, never a current result.
HISTORICAL_SCHEMA_VERSION = 1
#: Allowed values for a finding's disposition field.
_DISPOSITIONS = {"open", "fixed-pending-verification", "verified-fixed", "rejected"}
#: Severity tiers a finding may carry, mapped to the weight each adds to a round's weighted score.
_TIERS = {"security": 20, "critical": 10, "high": 6, "medium": 4, "low": 2, "nit": 1}
#: Dispositions that still count as open findings and contribute to the weighted score.
_OPEN_DISPOSITIONS = {"open", "fixed-pending-verification"}
#: Pattern for a lowercase 64-character hex SHA-256 digest, used to validate snapshot diff digests.
_DIGEST = re.compile(r"[0-9a-f]{64}")
#: Exact set of keys permitted at the top level of a loop ledger.
_ROOT_KEYS = {"schema_version", "implementation_author", "current_snapshot", "rounds"}
#: Exact set of keys permitted in each review round record.
_ROUND_KEYS = {"index", "reviewer", "snapshot", "report_path", "findings"}
#: Exact set of keys permitted in a round's reviewer record.
_REVIEWER_KEYS = {"identity", "independent"}
#: Exact set of keys permitted in a snapshot record (revision plus diff digest).
_SNAPSHOT_KEYS = {"revision", "diff_digest"}
#: Exact set of keys permitted in each finding record.
_FINDING_KEYS = {"signature", "tier", "structural", "disposition", "evidence"}
#: Schema revision of the per-round cost log; the log is advisory data, never an input to scoring or clean acceptance.
METRICS_SCHEMA_VERSION = 2
#: Cost-log revisions the appender still accepts; schema 1 predates ``tokens_total``.
_METRICS_SCHEMA_VERSIONS = (1, METRICS_SCHEMA_VERSION)
#: Exact set of keys permitted in each schema 1 per-round cost record.
_METRICS_KEYS_V1 = {
    "schema_version",
    "kind",
    "round",
    "route",
    "brief",
    "brief_bytes",
    "duration_s",
    "tokens_in",
    "tokens_out",
    "attested_gaps",
}
#: Exact set of keys permitted in each current per-round cost record.
_METRICS_KEYS = _METRICS_KEYS_V1 | {"tokens_total"}
#: Reviewer-brief wordings the cost log distinguishes; ``lean`` states each item once, ``full`` is the rule's brief.
_BRIEF_ARMS = ("full", "lean")
#: Cost fields that hold a nonnegative integer, or ``None`` when the route cannot report them.
_METRICS_COUNT_FIELDS = ("brief_bytes", "tokens_in", "tokens_out", "tokens_total", "attested_gaps")
#: Name of the file a parent drops beside the run directories to pause the lean brief arm for them.
_BRIEF_PAUSE_FILE = "brief-arm-pause"
#: Exact set of keys permitted at the top level of a round-actions record.
_ACTION_ROOT_KEYS = {"schema_version", "rounds"}
#: Exact set of keys permitted in each per-round entry of the actions record.
_ACTION_ROUND_KEYS = {"index", "actions"}
#: Exact set of keys permitted in a single action taken on a finding.
_ACTION_KEYS = {"signature", "decision", "evidence", "owner", "next_action", "root_cause"}
#: Keys every root-cause analysis attached to a fix action must provide.
_ROOT_CAUSE_KEYS = {"claim", "evidence", "falsification", "rejected_alternative"}
#: Allowed decisions for an action on a finding: fix it, escalate it, or defer it.
_ACTION_DECISIONS = {"fix", "escalate", "defer"}
#: Evidence prefix naming the invariant a finding violates; together with the signature it identifies a recurrence.
_INVARIANT_PREFIX = "invariant:"
#: Evidence prefix naming a first-reported finding's source origin (earlier snapshot, resolver edit, scope, undated).
_ORIGIN_PREFIX = "origin:"
#: The origin classes a later-round finding names after ``origin:``; anything else counts as missing.
_ORIGIN_CLASSES = ("earlier-snapshot", "resolver-edit", "scope-added", "undated")
#: Shape of a cost-log route: ``parent``, or ``agent:``/``bridge:``/``codex:``/``other:`` plus a label.
_ROUTE_SHAPE = re.compile(r"parent|(?:agent|bridge|codex|other):\S+")
#: Evidence prefixes a fix action must cover, each naming one kind of proof such as an invariant or a sibling.
_FIX_EVIDENCE_PREFIXES = (_INVARIANT_PREFIX, "original:", "consumer:", "sibling:", "source-paths:")


def _text(value: object) -> bool:
    """Return whether a value is a non-empty text field."""
    return isinstance(value, str) and bool(value.strip())


def rounds_path(ledger_path: Path) -> Path:
    """Return the append-only round log that belongs to one ledger header.

    Examples:
        >>> rounds_path(Path("run") / "loop-ledger.json").name
        'loop-rounds.jsonl'
        >>> rounds_path(Path("prior-loop-ledger.json")).name
        'prior-loop-rounds.jsonl'
    """
    name = ledger_path.name
    prefix = name[: -len("ledger.json")] if name.endswith("ledger.json") else f"{ledger_path.stem}-"
    return ledger_path.with_name(f"{prefix}rounds.jsonl")


def _read_rounds(path: Path) -> list[object]:
    """Read one JSON value per line from a round log; an absent log has no completed round."""
    if not path.is_file():
        return []
    rounds: list[object] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"rounds-line-blank:{number}")
        try:
            rounds.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"rounds-line-invalid-json:{number}") from error
    return rounds


def load_ledger(path: Path) -> object:
    """Assemble the ledger object that validation checks from its on-disk files.

    A schema-2 header gains the ``rounds`` list read from its sibling round log; a header that also carries inline
    rounds is rejected, because two copies of history could disagree. Any other JSON value, including a historical
    schema-1 file with inline rounds, is returned unchanged for validation to accept or reject.

    Raises:
        OSError: When the header or round log cannot be read.
        ValueError: When either file is not valid JSON or the header duplicates its round log.
    """
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return payload
    if "rounds" in payload:
        raise ValueError("ledger-header-inline-rounds-forbidden")
    return {**payload, "rounds": _read_rounds(rounds_path(path))}


def ledger_digest(path: Path) -> str:
    """Return the identity digest a continuation records for one ledger on disk.

    A historical single-file ledger keeps the digest of its exact bytes, so archived continuation records still verify.
    A schema-2 ledger digests the length-framed header bytes followed by its round-log bytes, so any change to either
    file, including whitespace, changes the identity. An absent round log contributes no bytes.
    """
    header = path.read_bytes()
    try:
        payload = json.loads(header)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return hashlib.sha256(header).hexdigest()
    log = rounds_path(path)
    log_bytes = log.read_bytes() if log.is_file() else b""
    return hashlib.sha256(len(header).to_bytes(8, "big") + header + log_bytes).hexdigest()


def _one_line(text: str) -> str:
    """Escape control characters so a warning built from record text stays one stderr line.

    Examples:
        >>> _one_line("sig\\nsecond-line")
        'sig\\\\nsecond-line'
        >>> _one_line("plain-code:x")
        'plain-code:x'
    """
    return ascii(text)[1:-1]


def _append_line(log: Path, record: dict[str, Any]) -> None:
    """Append one JSON record as its own line, first closing a final line an earlier writer left without a newline."""
    try:
        with log.open("rb") as handle:
            handle.seek(-1, 2)
            unterminated = handle.read(1) != b"\n"
    except OSError:
        unterminated = False
    with log.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(("\n" if unterminated else "") + json.dumps(record, sort_keys=True) + "\n")


def append_round(ledger_path: Path) -> list[str]:
    """Append the staged round record to the ledger's round log after validating the extended ledger.

    The owner writes one round object to ``<round log>.rec``; this action re-serializes it as one line, appends it, and
    deletes the staged file. Validation covers the whole extended ledger, so a non-contiguous index, a dropped earlier
    signature, or a round after a terminal stop is refused and nothing is appended.
    """
    log = rounds_path(ledger_path)
    staged = log.with_name(f"{log.name}.rec")
    try:
        record = json.loads(staged.read_bytes())
    except OSError:
        return [f"append-record-missing:{staged.name}"]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ["append-record-invalid-json"]
    if not isinstance(record, dict):
        return ["append-record-object-required"]
    try:
        ledger = load_ledger(ledger_path)
    except (OSError, ValueError) as error:
        return [f"ledger-read-failed:{error}"]
    if not isinstance(ledger, dict) or ledger.get("schema_version") != SCHEMA_VERSION:
        return ["append-requires-current-schema"]
    errors = validate_ledger({**ledger, "rounds": [*ledger["rounds"], record]})
    if errors:
        return errors
    warnings = _missing_origin_warnings(ledger["rounds"], record)
    _append_line(log, record)
    staged.unlink()
    for warning in warnings:
        print(f"warning: {_one_line(warning)}", file=sys.stderr)
    return []


def _missing_origin_warnings(earlier_rounds: list[dict[str, Any]], record: dict[str, Any]) -> list[str]:
    """Name open findings first reported after the baseline that carry no ``origin:`` evidence entry.

    The loop rule already requires the origin class of a later-round finding; only a handful of past reports recorded
    it, so the append action nudges without refusing. A baseline record never warns.

    Examples:
        >>> _missing_origin_warnings([], {"findings": []})
        []
    """
    if not earlier_rounds:
        return []
    seen: set[str] = set()
    for round_record in earlier_rounds:
        for keys in _history_keys(round_record):
            seen |= keys
    return [
        f"round-{record['index']}-finding-origin-missing:{finding['signature']}"
        for finding in record["findings"]
        if finding["disposition"] in _OPEN_DISPOSITIONS
        and not _identity_keys(finding["signature"], finding["evidence"]) & seen
        and not any(_names_origin_class(item) for item in finding["evidence"])
    ]


def _names_origin_class(item: object) -> bool:
    """Return whether one evidence entry reads ``origin: <class>`` with a class the loop rule defines.

    Examples:
        >>> _names_origin_class("origin: resolver-edit (round 2 diff)")
        True
        >>> [_names_origin_class(item) for item in ("origin:", "origin: banana", "invariant: x", 3)]
        [False, False, False, False]
    """
    if not isinstance(item, str) or not item.startswith(_ORIGIN_PREFIX):
        return False
    words = item[len(_ORIGIN_PREFIX) :].split()
    return bool(words) and words[0].rstrip(".,;:").casefold() in _ORIGIN_CLASSES


def metrics_path(ledger_path: Path) -> Path:
    """Return the append-only per-round cost log that belongs to one ledger header.

    Examples:
        >>> metrics_path(Path("run") / "loop-ledger.json").name
        'loop-metrics.jsonl'
        >>> metrics_path(Path("prior-loop-ledger.json")).name
        'prior-loop-metrics.jsonl'
    """
    rounds_name = rounds_path(ledger_path).name
    return ledger_path.with_name(f"{rounds_name[: -len('rounds.jsonl')]}metrics.jsonl")


def brief_arm(ledger_path: Path) -> str:
    """Assign the reviewer-brief wording for one run: ``lean`` or ``full``, balanced across runs and stable per run.

    The arm hashes the run directory's name, so the same run always gets the same arm and no counter file is needed. It
    only assigns wording for a pilot comparison; the parent records the brief it actually sent.

    Examples:
        >>> brief_arm(Path("2026-10-25T09-32-53Z") / "loop-ledger.json") in {"full", "lean"}
        True
        >>> brief_arm(Path("run-a") / "loop-ledger.json") == brief_arm(Path("elsewhere") / "run-a" / "loop-ledger.json")
        True
    """
    try:
        run = ledger_path.resolve().parent
        paused = (run.parent / _BRIEF_PAUSE_FILE).exists()
    except (OSError, ValueError, RuntimeError):
        # Every OS call is best-effort enrichment. Whatever it refuses (deleted working directory, symlink loop,
        # unsearchable parent, NUL byte), the arm falls back to the name as written and never raises.
        run = ledger_path.parent
        paused = False
    if paused:
        return "full"
    digest = hashlib.sha256(run.name.encode("utf-8", "surrogatepass")).hexdigest()
    return _BRIEF_ARMS[int(digest[0], 16) % 2]


def _count_or_unknown(value: object) -> bool:
    """Return whether a metric is a nonnegative integer or an honest unknown (``None``)."""
    return value is None or (type(value) is int and value >= 0)


def _seconds_or_unknown(value: object) -> bool:
    """Return whether a duration is a finite nonnegative number of seconds or an honest unknown (``None``)."""
    if type(value) is int:
        return value >= 0
    return value is None or (type(value) is float and math.isfinite(value) and value >= 0)


def _validate_metrics(record: object, completed_rounds: int) -> list[str]:
    """Check one staged metrics record: exact fields, supported version, a completed round, honest unknowns."""
    errors: list[str] = []
    stated = record.get("schema_version") if isinstance(record, dict) else None
    legacy = type(stated) is int and stated == 1
    metrics = _exact_keys(record, _METRICS_KEYS_V1 if legacy else _METRICS_KEYS, "metrics", errors)
    if metrics is None or errors:
        return errors
    if type(metrics["schema_version"]) is not int or metrics["schema_version"] not in _METRICS_SCHEMA_VERSIONS:
        errors.append("metrics-schema-version-unsupported")
    if metrics["kind"] != "round":
        errors.append("metrics-kind-invalid")
    index = metrics["round"]
    if not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= completed_rounds:
        errors.append("metrics-round-must-be-a-completed-round")
    if not _text(metrics["route"]):
        errors.append("metrics-route-required")
    if metrics["brief"] not in _BRIEF_ARMS:
        errors.append("metrics-brief-invalid")
    errors.extend(
        f"metrics-{field}-must-be-nonnegative-integer-or-null"
        for field in _METRICS_COUNT_FIELDS
        if field in metrics and not _count_or_unknown(metrics[field])
    )
    if not _seconds_or_unknown(metrics["duration_s"]):
        errors.append("metrics-duration_s-must-be-nonnegative-number-or-null")
    if index != 1 and metrics["attested_gaps"] is not None:
        errors.append("metrics-attested_gaps-only-on-round-1")
    return errors


def append_metrics(ledger_path: Path) -> list[str]:
    """Append the staged per-round cost record to the ledger's metrics log after validating it.

    The owner writes one metrics object to ``<metrics log>.rec``; this action validates it against the completed rounds
    of a valid current ledger, appends it as one line, and deletes the staged file. The log never feeds scoring,
    recurrence, or clean acceptance, so a rejected record leaves the ledger and round log untouched. A second record
    for one round supersedes the first: readers take the last record per round.
    """
    log = metrics_path(ledger_path)
    staged = log.with_name(f"{log.name}.rec")
    try:
        record = json.loads(staged.read_bytes())
    except OSError:
        return [f"append-metrics-record-missing:{staged.name}"]
    except (ValueError, RecursionError):  # bad bytes, bad JSON, an integer past the digit limit, absurd nesting
        return ["append-metrics-record-invalid-json"]
    try:
        ledger = load_ledger(ledger_path)
    except (OSError, ValueError) as error:
        return [f"ledger-read-failed:{error}"]
    if not isinstance(ledger, dict) or ledger.get("schema_version") != SCHEMA_VERSION:
        return ["append-metrics-requires-current-schema"]
    errors = validate_ledger(ledger) or _validate_metrics(record, len(ledger["rounds"]))
    if errors:
        return errors
    record["route"] = record["route"].strip()
    try:
        _append_line(log, record)
    except OSError as error:
        return [f"append-metrics-write-failed:{type(error).__name__}"]
    try:
        staged.unlink()
    except OSError as error:
        print(f"warning: metrics-staged-record-not-removed:{type(error).__name__}", file=sys.stderr)
    if _ROUTE_SHAPE.fullmatch(record["route"]) is None:
        print(f"warning: metrics-route-not-in-vocabulary:{record['route']!a}", file=sys.stderr)
    return []


def _exact_keys(value: object, expected: set[str], name: str, errors: list[str]) -> dict[str, Any] | None:
    """Validate an object has exactly the documented JSON fields."""
    if not isinstance(value, dict):
        errors.append(f"{name}-object-required")
        return None
    missing = expected - value.keys()
    unexpected = value.keys() - expected
    if missing:
        errors.append(f"{name}-missing-fields:{','.join(sorted(str(key) for key in missing))}")
    if unexpected:
        errors.append(f"{name}-unexpected-fields:{','.join(sorted(str(key) for key in unexpected))}")
    return value


def _validate_snapshot(value: object, name: str, errors: list[str]) -> dict[str, Any] | None:
    """Validate one named immutable snapshot coordinate."""
    snapshot = _exact_keys(value, _SNAPSHOT_KEYS, name, errors)
    if snapshot is None:
        return None
    if not _text(snapshot.get("revision")):
        errors.append(f"{name}-revision-required")
    digest = snapshot.get("diff_digest")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        errors.append(f"{name}-diff-digest-invalid")
    return snapshot


def _validate_finding(value: object, round_index: int, errors: list[str]) -> dict[str, Any] | None:
    """Validate one stable finding record without asserting provenance of its evidence."""
    finding = _exact_keys(value, _FINDING_KEYS, f"round-{round_index}-finding", errors)
    if finding is None:
        return None
    if not _text(finding.get("signature")):
        errors.append(f"round-{round_index}-finding-signature-required")
    tier = finding.get("tier")
    if not isinstance(tier, str) or tier not in _TIERS:
        errors.append(f"round-{round_index}-finding-tier-invalid")
    if not isinstance(finding.get("structural"), bool):
        errors.append(f"round-{round_index}-finding-structural-boolean-required")
    disposition = finding.get("disposition")
    if not isinstance(disposition, str) or disposition not in _DISPOSITIONS:
        errors.append(f"round-{round_index}-finding-disposition-invalid")
    evidence = finding.get("evidence")
    if not isinstance(evidence, list) or any(not _text(item) for item in evidence):
        errors.append(f"round-{round_index}-finding-evidence-list-required")
    elif isinstance(disposition, str) and disposition in {"rejected", "verified-fixed"} and not evidence:
        errors.append(f"round-{round_index}-{disposition}-finding-evidence-required")
    return finding


def _validate_rounds(payload: dict[str, Any], errors: list[str]) -> list[dict[str, Any]]:
    """Validate review-round shape, independence claims, and carried finding history."""
    value = payload.get("rounds")
    if not isinstance(value, list):
        errors.append("rounds-list-required")
        return []
    author = payload.get("implementation_author")
    prior_findings: dict[str, dict[str, Any]] = {}
    rounds: list[dict[str, Any]] = []
    for expected_index, value_round in enumerate(value, start=1):
        round_record = _exact_keys(value_round, _ROUND_KEYS, f"round-{expected_index}", errors)
        if round_record is None:
            continue
        rounds.append(round_record)
        index = round_record.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index != expected_index:
            errors.append("round-index-must-be-contiguous-integer")
        reviewer = _exact_keys(round_record.get("reviewer"), _REVIEWER_KEYS, f"round-{expected_index}-reviewer", errors)
        if reviewer is not None:
            if not _text(reviewer.get("identity")):
                errors.append(f"round-{expected_index}-reviewer-identity-required")
            if not isinstance(reviewer.get("independent"), bool):
                errors.append(f"round-{expected_index}-reviewer-independent-boolean-required")
            elif reviewer["independent"] and reviewer.get("identity") == author:
                errors.append(f"round-{expected_index}-self-review-forbidden")
        _validate_snapshot(round_record.get("snapshot"), f"round-{expected_index}-snapshot", errors)
        if not _text(round_record.get("report_path")):
            errors.append(f"round-{expected_index}-report-path-required")
        findings_value = round_record.get("findings")
        if not isinstance(findings_value, list):
            errors.append(f"round-{expected_index}-findings-list-required")
            continue
        current_findings: dict[str, dict[str, Any]] = {}
        for value_finding in findings_value:
            finding = _validate_finding(value_finding, expected_index, errors)
            if finding is None or not _text(finding.get("signature")):
                continue
            signature = finding["signature"]
            if signature in current_findings:
                errors.append(f"round-{expected_index}-finding-signature-duplicate:{signature}")
            current_findings[signature] = finding
        for signature, prior in prior_findings.items():
            current = current_findings.get(signature)
            if current is None:
                errors.append(f"round-{expected_index}-finding-dropped:{signature}")
            elif current.get("tier") != prior.get("tier"):
                errors.append(f"round-{expected_index}-finding-tier-changed:{signature}")
            elif current.get("structural") != prior.get("structural"):
                errors.append(f"round-{expected_index}-finding-structural-changed:{signature}")
        prior_findings = current_findings
    return rounds


def validate_ledger(payload: object) -> list[str]:
    """Return deterministic contract errors for one adversarial-review ledger."""
    errors: list[str] = []
    root = _exact_keys(payload, _ROOT_KEYS, "ledger", errors)
    if root is None:
        return errors
    version = root.get("schema_version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in {SCHEMA_VERSION, HISTORICAL_SCHEMA_VERSION}
    ):
        errors.append("unsupported-schema-version")
    if not _text(root.get("implementation_author")):
        errors.append("implementation-author-required")
    _validate_snapshot(root.get("current_snapshot"), "current-snapshot", errors)
    rounds = _validate_rounds(root, errors)
    if not errors:
        summary = _summary_from_valid(root)
        reviewed_rounds = summary["rounds"]
        if not isinstance(reviewed_rounds, list):
            raise TypeError(f"reviewed_rounds must be list, got {type(reviewed_rounds).__name__}")
        terminal_round = len(reviewed_rounds)
        if summary["reason"] == "independence-unavailable":
            author = root["implementation_author"]
            if not isinstance(author, str):
                raise TypeError(f"author must be str, got {type(author).__name__}")
            terminal_round = next(
                (
                    index
                    for index, round_record in enumerate(rounds, start=1)
                    if not _is_independent_round(round_record, author)
                ),
                0,
            )
        if summary["status"] == "stopped" and len(rounds) > terminal_round:
            errors.append("round-after-stop")
    return errors


def _is_independent_round(round_record: dict[str, Any], author: str) -> bool:
    """Return whether a structurally valid record declares non-self independent coverage."""
    reviewer = round_record.get("reviewer")
    return (
        isinstance(reviewer, dict)
        and reviewer.get("independent") is True
        and _text(reviewer.get("identity"))
        and reviewer.get("identity") != author
    )


def _score(round_record: dict[str, Any]) -> int:
    """Compute the weighted total of open and pending-verification findings."""
    findings = round_record["findings"]
    return sum(_TIERS[finding["tier"]] for finding in findings if finding["disposition"] in _OPEN_DISPOSITIONS)


def _summary_from_valid(payload: dict[str, Any]) -> dict[str, object]:
    """Infer a status from a structurally valid ledger without rerunning validation."""
    rounds = payload["rounds"]
    if not isinstance(rounds, list):
        raise TypeError(f"rounds must be list, got {type(rounds).__name__}")
    if not rounds:
        return {"status": "stopped", "reason": "independence-unavailable", "scores": [], "rounds": []}

    author = payload["implementation_author"]
    if not isinstance(author, str):
        raise TypeError(f"author must be str, got {type(author).__name__}")
    scores: list[int] = []
    round_summaries: list[dict[str, object]] = []
    for position, round_record in enumerate(rounds, start=1):
        if not isinstance(round_record, dict):
            raise TypeError(f"round_record must be dict, got {type(round_record).__name__}")
        if not _is_independent_round(round_record, author):
            return {
                "status": "stopped",
                "reason": "independence-unavailable",
                "scores": scores,
                "rounds": round_summaries,
            }
        findings = round_record["findings"]
        if not isinstance(findings, list):
            raise TypeError(f"findings must be list, got {type(findings).__name__}")
        counts = {tier: 0 for tier in _TIERS}
        for finding in findings:
            if finding["disposition"] in _OPEN_DISPOSITIONS:
                counts[finding["tier"]] += 1
        score = _score(round_record)
        scores.append(score)
        decision = "baseline" if position == 1 else "converging"
        if score == 0:
            snapshot = round_record["snapshot"]
            if snapshot != payload["current_snapshot"]:
                decision = "stale-review"
            else:
                decision = "clean"
        elif position > 1:
            previous_score = scores[-2]
            if score > previous_score:
                decision = "nonconverging"
            elif score == previous_score:
                decision = "plateau"
        round_summaries.append({"index": position, "score": score, "counts": counts, "decision": decision})
        if decision not in {"baseline", "converging"}:
            return {"status": "stopped", "reason": decision, "scores": scores, "rounds": round_summaries}
    return {"status": "active", "reason": round_summaries[-1]["decision"], "scores": scores, "rounds": round_summaries}


def summarize_ledger(payload: object) -> dict[str, object]:
    """Summarize a valid ledger or raise ValueError with its validation errors."""
    errors = validate_ledger(payload)
    if errors:
        raise ValueError(";".join(errors))
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return _summary_from_valid(payload)


def validate_actions(ledger: object, payload: object) -> list[str]:
    """Bind parent triage and resolution evidence to each open reviewed finding.

    Schema 3 counts recurrence per invariant: an action's identity is its signature plus every ``invariant:`` evidence
    text in its matching ledger finding; every action invariant must also appear there after normalization. Identity
    includes keys connected transitively through earlier findings, regardless of disposition. A reworded invariant
    under a known signature and a new route-named signature under a known invariant are therefore both
    repeats. All dispositions enter history; findings sharing a key inside one round count once.
    Schemas 1 and 2 count signatures only, requiring root cause for a fix repeated from the previous round and stopping
    a fix after two earlier open occurrences.
    """
    errors = validate_ledger(ledger)
    if errors:
        return errors
    if not isinstance(ledger, dict):
        raise TypeError(f"ledger must be dict, got {type(ledger).__name__}")
    root = _exact_keys(payload, _ACTION_ROOT_KEYS, "actions", errors)
    if root is None:
        return errors
    action_version = root.get("schema_version")
    # Schema 3 is current; schemas 1 and 2 stay readable as historical records under their own evidence rules.
    if not isinstance(action_version, int) or isinstance(action_version, bool) or action_version not in {1, 2, 3}:
        errors.append("actions-schema-version-invalid")
    rounds = root.get("rounds")
    if not isinstance(rounds, list) or len(rounds) != len(ledger["rounds"]):
        errors.append("actions-rounds-mismatch")
        return errors
    # Publish each finding's key connections only after checking its whole round. Recurrence can follow historical
    # chains without treating an action's same-round sibling as an earlier occurrence.
    rounds_by_key: dict[str, set[int]] = {}
    related_keys: dict[str, set[str]] = {}
    prior_open: set[str] = set()
    for index, (review, action_value) in enumerate(zip(ledger["rounds"], rounds, strict=True), start=1):
        action_keys = _validate_action_round(
            review, action_value, index, action_version, rounds_by_key, related_keys, prior_open, errors
        )
        if action_version == 3:
            action_keys = _history_keys(review)
        for keys in action_keys:
            for key in keys:
                rounds_by_key.setdefault(key, set()).add(index)
                if action_version == 3:
                    related_keys.setdefault(key, set()).update(keys)
    return errors


def _validate_action_round(
    review: dict[str, Any],
    action_value: object,
    index: int,
    action_version: object,
    rounds_by_key: dict[str, set[int]],
    related_keys: dict[str, set[str]],
    prior_open: set[str],
    errors: list[str],
) -> list[set[str]]:
    """Check one round's actions against its open findings and return each action's connected identity keys."""
    action_keys: list[set[str]] = []
    action_round = _exact_keys(action_value, _ACTION_ROUND_KEYS, f"round-{index}-actions", errors)
    if action_round is None:
        return action_keys
    if action_round.get("index") != index or isinstance(action_round.get("index"), bool):
        errors.append(f"round-{index}-actions-index-invalid")
    items = action_round.get("actions")
    if not isinstance(items, list):
        errors.append(f"round-{index}-actions-list-required")
        return action_keys
    open_findings = {
        finding["signature"]: finding for finding in review["findings"] if finding["disposition"] in _OPEN_DISPOSITIONS
    }
    observed: set[str] = set()
    for value in items:
        action = _exact_keys(value, _ACTION_KEYS, f"round-{index}-action", errors)
        if action is None:
            continue
        signature = action.get("signature")
        if not _text(signature):
            errors.append(f"round-{index}-action-signature-required")
            continue
        if signature in observed:
            errors.append(f"round-{index}-action-duplicate:{signature}")
        observed.add(signature)
        finding = open_findings.get(signature)
        if finding is None:
            errors.append(f"round-{index}-action-not-open:{signature}")
            continue
        _validate_action_record(action, finding, index, action_version, errors)
        keys = _identity_keys(signature, action.get("evidence"))
        if action_version == 3:
            # Ledger evidence defines identity for both recurrence and progress; actions cannot add unseen keys.
            finding_keys = _identity_keys(signature, finding["evidence"])
            if not keys <= finding_keys:
                errors.append(f"round-{index}-action-invariant-not-in-finding:{signature}")
            keys = finding_keys
            errors.extend(_recurrence_errors(action, keys, index, rounds_by_key, related_keys))
        else:
            errors.extend(_legacy_recurrence_errors(action, index, prior_open, rounds_by_key))
        action_keys.append(keys)
    for signature in open_findings.keys() - observed:
        errors.append(f"round-{index}-action-missing:{signature}")
        action_keys.append({f"signature:{signature}"})
    prior_open.clear()
    prior_open.update(open_findings)
    return action_keys


def _validate_action_record(
    action: dict[str, Any], finding: dict[str, Any], index: int, action_version: object, errors: list[str]
) -> None:
    """Check one action's decision, evidence, ownership and root cause against the open finding it answers."""
    signature = action["signature"]
    decision = action.get("decision")
    if not isinstance(decision, str) or decision not in _ACTION_DECISIONS:
        errors.append(f"round-{index}-action-decision-invalid:{signature}")
    if finding["tier"] in {"security", "critical", "high"} and decision == "defer":
        errors.append(f"round-{index}-severe-finding-must-fix-or-escalate:{signature}")
    evidence = action.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(not _text(item) for item in evidence):
        errors.append(f"round-{index}-action-evidence-required:{signature}")
    else:
        kind = "fix" if decision == "fix" else "action"
        errors.extend(
            f"round-{index}-{kind}-evidence-missing:{prefix}{signature}"
            for prefix in _missing_evidence_prefixes(evidence, decision, action_version)
        )
    for field in ("owner", "next_action"):
        if not _text(action.get(field)):
            errors.append(f"round-{index}-action-{field}-required:{signature}")
    root_cause = action.get("root_cause")
    if root_cause is not None:
        cause = _exact_keys(root_cause, _ROOT_CAUSE_KEYS, f"round-{index}-root-cause", errors)
        if cause is not None:
            for field in _ROOT_CAUSE_KEYS:
                if not _text(cause.get(field)):
                    errors.append(f"round-{index}-root-cause-{field}-required:{signature}")


def _missing_evidence_prefixes(evidence: list[str], decision: object, action_version: object) -> list[str]:
    """Return the evidence prefixes an action's schema requires but its evidence lacks or leaves empty.

    Schema 3 requires an ``invariant:`` entry on every decision, so an escalated or deferred finding still has the
    identity its recurrence is counted under. A ``fix`` in schema 2 or 3 also names its original case, consumer check,
    sibling route and source paths. Schema 1 required no prefix.

    Examples:
        >>> _missing_evidence_prefixes(["original: retained case"], "escalate", 3)
        ['invariant:']
        >>> _missing_evidence_prefixes(["original: retained case"], "escalate", 2)
        []
        >>> _missing_evidence_prefixes(["invariant: arms judged", "sibling: none"], "fix", 3)
        ['original:', 'consumer:', 'source-paths:']
    """
    required: tuple[str, ...] = ()
    if decision == "fix" and action_version in {2, 3}:
        required = _FIX_EVIDENCE_PREFIXES
    elif action_version == 3:
        required = (_INVARIANT_PREFIX,)
    return [
        prefix
        for prefix in required
        if not any(item.startswith(prefix) and item[len(prefix) :].strip() for item in evidence)
    ]


def _legacy_recurrence_errors(
    action: dict[str, Any], index: int, prior_open: set[str], rounds_by_key: dict[str, set[int]]
) -> list[str]:
    """Apply the signature-only repeat and occurrence rules of actions schemas 1 and 2."""
    if action.get("decision") != "fix":
        return []
    signature = action["signature"]
    errors: list[str] = []
    if signature in prior_open and action.get("root_cause") is None:
        errors.append(f"round-{index}-repeat-root-cause-required:{signature}")
    if len(rounds_by_key.get(f"signature:{signature}", set())) >= 2:
        errors.append(f"round-{index}-third-occurrence-must-stop:{signature}")
    return errors


def _recurrence_errors(
    action: dict[str, Any],
    keys: set[str],
    index: int,
    rounds_by_key: dict[str, set[int]],
    related_keys: dict[str, set[str]],
) -> list[str]:
    """Return the repeat and third-occurrence errors for one action against every earlier round.

    The earlier occurrences are the rounds holding any key transitively connected to the action's keys by findings of
    earlier rounds. A ``fix`` of a repeat needs root-cause evidence; a ``fix`` once two earlier rounds carried the
    invariant is its third occurrence and must stop instead. Other decisions already stop or defer the fix.
    """
    if action.get("decision") != "fix":
        return []
    signature = action["signature"]
    connected = set(keys)
    pending = list(keys)
    while pending:
        neighbors = related_keys.get(pending.pop(), set()) - connected
        connected.update(neighbors)
        pending.extend(neighbors)
    earlier_rounds = set().union(*(rounds_by_key.get(key, set()) for key in connected))
    errors: list[str] = []
    if earlier_rounds and action.get("root_cause") is None:
        errors.append(f"round-{index}-repeat-root-cause-required:{signature}")
    if len(earlier_rounds) >= 2:
        errors.append(f"round-{index}-third-occurrence-must-stop:{signature}")
    return errors


def _identity_keys(signature: str, evidence: object) -> set[str]:
    """Return the keys a finding's recurrence is counted under: its signature and every invariant its evidence names.

    The invariant text is compared with blanks collapsed and case folded, so the parent reusing the same invariant
    wording for a sibling route makes the two one recurring invariant, while the signature key still catches a reworded
    invariant on a known signature.

    Examples:
        >>> sorted(_identity_keys("route-a", ["invariant: Every  arm judged", "original: x"]))
        ['invariant:every arm judged', 'signature:route-a']
        >>> sorted(_identity_keys("route-a", ["Changed parser boundary."]))
        ['signature:route-a']
    """
    keys = {f"signature:{signature}"}
    for item in evidence if isinstance(evidence, list) else []:
        if isinstance(item, str) and item[: len(_INVARIANT_PREFIX)].casefold() == _INVARIANT_PREFIX:
            text = " ".join(item[len(_INVARIANT_PREFIX) :].split()).casefold()
            if text:
                keys.add(f"{_INVARIANT_PREFIX}{text}")
    return keys


def _history_keys(round_record: dict[str, Any]) -> list[set[str]]:
    """Retain every finding's identity in history, including verified-fixed and rejected findings."""
    return [_identity_keys(finding["signature"], finding["evidence"]) for finding in round_record["findings"]]


def plateau_kind(payload: dict[str, Any]) -> str | None:
    """Name why the latest round did not lower the score, or ``None`` when it did (or none ran).

    ``stuck`` means every open finding was already seen in an earlier round (carried or reopened), so the fix did not
    land and needs a different approach. ``whack-a-mole`` means every open finding is new, so the fix closed its target
    but the touched area still hides siblings. ``mixed`` is both. The result is informational and never changes the
    ledger's decision.

    Examples:
        >>> plateau_kind({"rounds": []}) is None
        True
    """
    rounds = payload["rounds"]
    if len(rounds) < 2:
        return None
    scores = [_score(round_record) for round_record in rounds]
    if scores[-1] == 0 or scores[-1] < scores[-2]:
        return None
    seen: set[str] = set()
    for round_record in rounds[:-1]:
        for keys in _history_keys(round_record):
            seen |= keys
    open_findings = [finding for finding in rounds[-1]["findings"] if finding["disposition"] in _OPEN_DISPOSITIONS]
    carried = sum(1 for finding in open_findings if _identity_keys(finding["signature"], finding["evidence"]) & seen)
    fresh = len(open_findings) - carried
    if carried and not fresh:
        return "stuck"
    return "whack-a-mole" if fresh and not carried else "mixed"


def _render_progress(payload: dict[str, Any], completed_rounds: int) -> str:
    """Render validated completed history with separate old and new open totals."""
    if not completed_rounds:
        return ""
    lines = [
        "| Iteration | Security | Critical | High | Medium | Low | Nits | Weighted score |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    seen: set[str] = set()
    for round_record in payload["rounds"][:completed_rounds]:
        counts = {tier: [0, 0] for tier in ("security", "critical", "high", "medium", "low", "nit")}
        weighted = [0, 0]
        for finding in round_record["findings"]:
            if finding["disposition"] not in _OPEN_DISPOSITIONS:
                continue
            bucket = 0 if _identity_keys(finding["signature"], finding["evidence"]) & seen else 1
            tier = finding["tier"]
            counts[tier][bucket] += 1
            weighted[bucket] += _TIERS[tier]
        cells = [str(round_record["index"]), *(f"{old} + {new}" for old, new in [*counts.values(), weighted])]
        lines.append("| " + " | ".join(cells) + " |")
        for keys in _history_keys(round_record):
            seen |= keys
    lines.append("")
    lines.append(
        "Cells: old + new open findings (including pending verification); old = signature or `invariant:` evidence seen "
        "in any prior completed round. Security and Critical count their own tiers; weights: security 20, critical 10, "
        "high 6, medium 4, low 2, nit 1. Weighted score: old weighted + new weighted."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Emit JSON validation and optionally a cumulative human-readable progress table.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``. Claude plugins' ``bin/`` contract tests pass them.
    """
    parser = argparse.ArgumentParser(description="Validate an adversarial-review convergence ledger.")
    parser.add_argument("--ledger", required=True, type=Path, help="Ledger header JSON path; rounds sit beside it.")
    parser.add_argument("--require-clean", action="store_true", help="Exit nonzero unless the valid summary is clean.")
    parser.add_argument("--progress", action="store_true", help="Repeat the cumulative progress table on stderr.")
    parser.add_argument("--actions", type=Path, help="Validate per-finding parent actions against the ledger.")
    parser.add_argument(
        "--append",
        action="store_true",
        help="First append the staged round from <round log>.rec, refusing it unless the extended ledger is valid.",
    )
    parser.add_argument(
        "--append-metrics",
        action="store_true",
        help="After any --append, append the staged cost record from <metrics log>.rec for a completed round.",
    )
    parser.add_argument(
        "--plateau-kind",
        action="store_true",
        help="Print stuck, whack-a-mole, mixed or none on stderr for the latest round; informational only.",
    )
    parser.add_argument(
        "--brief-arm",
        action="store_true",
        help="Print the pilot reviewer-brief wording (full or lean) for this run and exit; the ledger need not exist.",
    )
    args = parser.parse_args(argv)
    if args.brief_arm:
        if (
            args.append
            or args.append_metrics
            or args.require_clean
            or args.progress
            or args.plateau_kind
            or args.actions
        ):
            parser.error("--brief-arm cannot be combined with another action")
        print(brief_arm(args.ledger))
        return 0
    if args.append:
        append_errors = append_round(args.ledger)
        if append_errors:
            print(
                json.dumps({"status": "invalid", "reason": "append-rejected", "errors": append_errors}, sort_keys=True),
                file=sys.stderr,
            )
            return 1
    if args.append_metrics:
        # Advisory data: a rejected cost record must not hide the committed round's summary or progress table.
        try:
            metrics_errors = append_metrics(args.ledger)
        except Exception as error:
            metrics_errors = [f"append-metrics-failed:{type(error).__name__}"]
        if metrics_errors:
            print(f"warning: metrics-rejected:{_one_line(','.join(metrics_errors))}", file=sys.stderr)
    try:
        payload = load_ledger(args.ledger)
    except (OSError, ValueError) as error:
        print(
            json.dumps({"status": "invalid", "reason": "ledger-read-failed", "errors": [str(error)]}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    errors = validate_ledger(payload)
    if not errors and args.actions is not None:
        try:
            with args.actions.open(encoding="utf-8") as handle:
                actions = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"actions-read-failed:{error}")
        else:
            errors.extend(validate_actions(payload, actions))
    if errors:
        print(
            json.dumps({"status": "invalid", "reason": "invalid-ledger", "errors": errors}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    summary = summarize_ledger(payload)
    if args.progress and summary["rounds"]:
        print(_render_progress(payload, len(summary["rounds"])), file=sys.stderr)
    if args.plateau_kind:
        stopped_on_score = summary["reason"] in ("plateau", "nonconverging")
        print(f"plateau-kind: {(stopped_on_score and plateau_kind(payload)) or 'none'}", file=sys.stderr)
    print(json.dumps(summary, sort_keys=True))
    return 0 if not args.require_clean or summary["reason"] == "clean" else 1


if __name__ == "__main__":
    raise SystemExit(main())
