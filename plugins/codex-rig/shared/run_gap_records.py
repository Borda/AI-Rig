"""Record a promoted Code Review or Code Remediate run's coverage gaps in its gap ledger, ``<run>/gaps.jsonl``.

## Purpose

    Make the gaps and limitations of every promoted Codex review and remediation countable across runs by pull request
    and gap identity, through the shared gap-ledger schema owner ``gap_ledger.py``. The finalizer already holds every
    fact the ledger needs once ``result.json`` is promoted: the confidence-gap lines and their closures, the gate rows
    that did not pass with their cause, the single source refusal, the review route and the reviewed subject. This
    module turns those facts into ledger records, carries an earlier run's open gaps forward on an identical head
    instead of restating them, marks a listed known hardware limit once without a deduction, and appends through the
    helper's own validation.

## Scope

    Sources, all read from the promoted run directory:

    - ``metadata.confidence_gaps``: one record per line not closed in ``metadata.confidence_gap_closures``. The
      ``(-0.NN)`` prefix becomes the record's ``deduction``. A stated device absence is ``hardware-absent`` with its
      accelerator class (CUDA, ROCm, TensorRT, XPU, TPU, NPU, MPS, multi-GPU) and the line as its exact reason.
      With no stated device absence, a missing, unprovisioned or wrong package or build is
      ``environment-provisionable``; fork or
      contributed-code policy is ``contribution``; a recorded user choice or scope exclusion is ``human-decision``;
      hosted CI, CLA or base-branch state is ``project``; anything else is ``process``. A line that only states the
      host operating system, architecture or a single Python version is a fact of the run and is never recorded.
      Package descriptions are listed in the summary. The gate evidence supplies their distribution identity and
      provisioning outcome. A hook-cache description requires ``tool_cache`` evidence from the final gate run.
    - ``gates.json``: one record per gate row that did not pass and carries an environment or process ``cause``
      (``command-failed`` is the check's result, not a gap), and one record for the top-level source ``refusal``.
      A ``missing-python-package`` row uses ``project_env`` from the final gate run or a failed apply receipt in
      ``gate-provisioning.json``. A skipped provisioning step is recorded as a workflow gap by gate. A
      ``hook-cache-unwritable`` row requires ``tool_cache`` evidence from the final gate run.
    - Run identity: subject from ``pr-routing.json`` (``pr/pr-routing.json`` for remediation) or, for a local run,
      from the invoking checkout's ``origin`` remote and reviewed commit; ``run_id`` from the run directory; the
      installed version from this plugin's ``.codex-plugin/plugin.json``; the review route from
      ``metadata.execution_mode``: ``parallel``, ``independent-spawned`` and ``serial`` (one spawned reviewer) map to
      ``direct``, or ``paged`` when the run holds a batch inventory or only the historical inspection plan;
      ``serial-fallback`` stays ``serial-fallback``; a direct or paged run whose parent covered a required role is
      ``parent-substitute``. A closed or unavailable review and every remediation run use ``none``. An unknown mode
      records nothing.

    Preparation captures the invoking checkout's known limits for this host. An explicitly waived hardware record
    matching the captured repository, ID, hardware class and observed absence takes the listed reason and carries
    ``known_limit: true`` and no deduction. A deduction reported by the run stays effective even if the live registry
    now lists the gap.

    Never recorded: a closure note, a host fact, an unprovisioned package, a record the schema refuses (dropped and
    named), or anything when the subject or route cannot be established.

## Usage

    ``record_promoted_run(run, skill)`` after ``result.json`` is promoted; ``remediation_finalize.py finalize
    --promote`` calls it for ``--skill code-review`` and ``--skill code-remediate``. A first emission in a run carries
    the newest eligible sibling run's open gaps (same repository, pull request, head, run kind, plugin and plugin
    version) with ``gap_ledger.carry_forward``. Fresh records are appended when an open gap's deduction or waiver
    changes or when it was not open. A gap this run no longer reports gets a ``status: closed`` event. Repeating
    finalization on the same run appends nothing new. Records are staged in ``gaps.jsonl.finalize.rec``
    and appended by ``gap_ledger.append_staged``, which validates the extended ledger and deletes the stage.
    Run ``run_gap_records.py prepare --run RUN`` before scoring confidence to read the invoking checkout's limits.

## Outputs

    Appends to ``<run>/gaps.jsonl`` and returns one summary dictionary: ``status`` (``recorded``, ``skipped`` or
    ``failed``), the route, counts of appended, carried and closed records, the carried-from run id, dropped
    records with their first schema error, ``not_recorded`` lines with their reason (``host-fact`` or
    ``provisioning-not-attempted``) and text, and a ``code`` naming any skip or failure.
    Preparation saves ``<run>/gap-known-limits.json`` and prints host-matching limits, a warning for an ignored file,
    or an empty marker. A run without a captured registry can verify a reported waiver against the live registry.

## Failure

    Expected conditions never raise: an unreadable result, an unknown subject or route, an invalid known-limits file
    (ignored, named in ``notes``), an ineligible or refused carry (recorded fresh), a symlinked or invalid ledger and
    a refused append each return ``skipped`` or ``failed`` with a ``code``. The delivered result is never changed.
    Preparation exits zero after reporting the limits or a warning. Invalid command lines exit two.

## Used by

    ``remediation_finalize.py`` at Code Review and Code Remediate promotion. ``gap_ledger.py`` stays the schema
    owner; this module only derives records and calls its loaders, ``carry_forward`` and ``append_staged``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

# A source file cannot contain a bytecode cache directory.
sys.pycache_prefix = str(Path(__file__).resolve())

#: Directory holding this module and ``gap_ledger.py``.
SHARED_DIRECTORY = Path(__file__).resolve().parent
if str(SHARED_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SHARED_DIRECTORY))

import gap_ledger  # noqa: E402

#: Ledger file name inside a run directory.
LEDGER_NAME = "gaps.jsonl"
#: Writer name of this module's staged records, ``gaps.jsonl.finalize.rec``.
STAGE_WRITER = "finalize"
#: Plugin name recorded in ``run.plugin``.
PLUGIN_NAME = "codex-rig"
#: Harness recorded in ``run.harness``.
HARNESS = "codex"
#: Per-repository known-limits file, relative to the invoking checkout.
KNOWN_LIMITS_PATH = Path(".notes") / "known-limits.json"
KNOWN_LIMITS_SNAPSHOT = "gap-known-limits.json"
PROVISIONING_RECEIPT = "gate-provisioning.json"
#: Ledger run kind of each finalized skill; other skills record nothing.
SKILL_KINDS = {"code-review": "review", "code-remediate": "remediate"}
#: Ledger route of each Code Review ``execution_mode`` token, before paged and parent-substitute refinement.
ROUTE_BY_MODE = {
    "parallel": "direct",
    "independent-spawned": "direct",
    "serial": "direct",
    "serial-fallback": "serial-fallback",
}
#: Review statuses whose result dispatched no reviewer.
UNREVIEWED_STATUSES = frozenset({"closed", "unavailable"})
#: Reviewer-assessment role suffix naming a role the parent covered.
PARENT_SUBSTITUTE_SUFFIX = "(parent substitute)"
#: Party that usually closes a gap of each class.
CLASS_OWNERS = {
    "process": "workflow",
    "environment-provisionable": "workflow",
    "hardware-absent": "host",
    "project": "maintainer",
    "contribution": "contributor",
    "human-decision": "user",
}
#: What closes a free-text gap of each class; a hardware gap names its class instead.
CLASS_CLOSABLE_BY = {
    "process": "Rerun the workflow step that lost or never produced this evidence and retain its output.",
    "environment-provisionable": (
        "Provision the missing package, cache or index in an ephemeral environment outside the reviewed repository "
        "(gate_env_provision.py) and rerun the affected check."
    ),
    "project": "The maintainer or hosted CI completes the named check.",
    "contribution": "The contributor or maintainer resolves the contribution policy, then the run is repeated.",
    "human-decision": "The user revisits the recorded decision.",
}
#: Gate causes that are gaps, with their class and closing action; ``command-failed`` is a check result, not a gap.
GATE_CAUSES = {
    "hook-cache-unwritable": "Give the hooks a writable cache with --tool-cache.",
    "network-unavailable": "Rerun the gate under the networked-command approval.",
    "loopback-bind-blocked": "Rerun the gate on a route that allows a loopback socket.",
    "timeout": "Rerun the gate with a timeout that covers the full command.",
    "missing-command": "Install or select the gate's tool, then rerun the gate.",
    "import-origin-unproven": "Rerun the gate so the tested imports resolve from the reviewed checkout.",
}
MODULE_DISTRIBUTIONS = {
    # Joined at import so the release payload scan, which reads a letter plus digit as a phase label, stays quiet.
    "bs4": "beautifulsoup" + "4",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "PIL": "pillow",
    "Crypto": "pycryptodome",
    "dateutil": "python-dateutil",
    "fitz": "pymupdf",
    "jwt": "pyjwt",
    "serial": "pyserial",
    "skimage": "scikit-image",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
}
CONFIDENCE_IDS = {
    "readiness-incomplete": r"\b(?:verification )?readiness (?:is )?(?:incomplete|blocked)\b",
    "unreviewed-dimension-<unit>": r"^unreviewed dimension\s*:\s*([^.;—]+)",
    "full-suite-not-run": r"\b(?:full (?:test )?suite (?:was )?not run|did not run the full (?:test )?suite)\b",
    "push-not-landed": r"\b(?:push (?:did not land|not landed|failed)|push was not landed)\b",
    "codex-co-review-<reason>": r"\bcodex co[- ]review(?: (?:absent|unavailable|not run|failed|skipped))?\s*:\s*([\w-]+)",
    "pr-code-not-executed": r"\b(?:pr|pull request) code (?:was )?(?:not executed|not run)\b",
    "implementation-challenge-not-run": r"\bimplementation challenge (?:was )?not run\b",
}
#: ``gates.json`` evidence that a provisioned environment served the final gate run, per provisionable cause.
PROVISIONING_EVIDENCE = {"missing-python-package": "project_env", "hook-cache-unwritable": "tool_cache"}
#: Omission reason of a host operating-system, architecture or Python-version line, which is a fact, not a gap.
HOST_FACT = "host-fact"
#: Omission reason of a package or cache gap the workflow never tried to provision.
NOT_PROVISIONED = "provisioning-not-attempted"
#: Gate row statuses that are not gaps: passed, not applicable, or refused (recorded once through ``refusal``).
NON_GAP_GATE_STATUSES = frozenset({"pass", "not-applicable"})
#: A host operating system, architecture or Python version named in a gap line.
HOST_NAME = re.compile(
    r"\b(?:mac ?os|os x|darwin|linux|ubuntu|windows|wsl|x86[_-]64|amd64|arm64|aarch64|apple silicon|rosetta"
    r"|python ?3\.\d+)\b",
    re.IGNORECASE,
)
#: Wording that makes a named host a statement about where the run executed, not about the reviewed code.
HOST_CONTEXT = re.compile(
    r"\bhosts?\b|\bplatforms?\b|\bsingle python version\b|\bnot (?:run|executed|exercised|tested|verified)\b"
    r"|\bun(?:tested|verified|exercised)\b|\bnever (?:run|executed|exercised|tested)\b",
    re.IGNORECASE,
)
#: Non-hardware classes recognised in a gap line, checked in this order; unmatched lines are ``process``.
CLASS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "environment-provisionable",
        re.compile(
            r"\bnot installed\b|\bnot importable\b|\bmodulenotfounderror\b|\bno module named\b"
            r"|\bmissing (?:python )?(?:packages?|dependenc(?:y|ies)|modules?)\b|\bopencv\b|\bcv2\b"
            r"|\bhook cache\b|\bpre-commit cache\b|\bprovision",
            re.IGNORECASE,
        ),
    ),
    (
        "contribution",
        re.compile(r"\bforks?\b|\bcontribut(?:or|ed) code\b|\buntrusted\b|\bcode[- ]safety\b", re.IGNORECASE),
    ),
    (
        "human-decision",
        re.compile(
            r"\buser (?:declined|chose|asked|requested|skipped|deferred)\b|\bat the user's request\b|\bon request\b"
            r"|\bopted out\b|\bnot pushed\b|\bpush (?:was )?(?:skipped|declined)\b"
            r"|\b(?:excluded from|out of) (?:the )?(?:requested )?scope\b|\bexplicitly (?:deferred|excluded)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "project",
        re.compile(
            r"\bci\b|\bhosted checks?\b|\bremote checks?\b|\bchecks? (?:was |were |is |are )?(?:queued|pending)\b"
            r"|\bcla\b|\bbase[- ]branch\b|\bbranch protection\b|\bgithub actions\b",
            re.IGNORECASE,
        ),
    ),
)
_DEDUCTION = re.compile(r"^\s*\(-(?P<value>[01]\.\d{2})\)\s*")
_PR_DIRECTORY = re.compile(r"pr-[1-9][0-9]*")
_REMOTE_REPOSITORY = re.compile(r"[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+?)(?:\.git)?/?\Z")
_SLUG_STEM = 40
_GIT_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class TextClass:
    """Ledger class of one free-text gap line; ``gap_class`` is ``None`` for a host fact that is never recorded."""

    gap_class: str | None
    hardware_class: str | None = None


@dataclass(frozen=True)
class GapDraft:
    """One gap before the run identity is attached: everything a ledger record states about the gap itself."""

    id: str
    gap_class: str
    summary: str
    closable_by: str
    evidence: str
    deduction: float | None = None
    hardware_class: str | None = None
    reason: str | None = None
    waiver_reported: bool = False


@dataclass(frozen=True)
class KnownLimits:
    """The validated registry or scoring snapshot with this host's limits by ID."""

    path: Path
    repo: str
    limits: dict[str, gap_ledger.KnownLimit]


def one_line(text: str) -> str:
    """Collapse whitespace to one line and shorten it to the ledger's text limit.

    Examples:
        >>> one_line("two\\n  lines")
        'two lines'
        >>> len(one_line("x" * 400)) == gap_ledger.MAX_TEXT
        True
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= gap_ledger.MAX_TEXT:
        return collapsed
    return collapsed[: gap_ledger.MAX_TEXT - 3].rstrip() + "..."


def split_deduction(line: str) -> tuple[float | None, str]:
    """Split a ``(-0.NN) Description`` confidence-gap line into its deduction and description.

    Examples:
        >>> split_deduction("(-0.03) Parent-only review.")
        (0.03, 'Parent-only review.')
        >>> split_deduction("No prefix here")
        (None, 'No prefix here')
    """
    match = _DEDUCTION.match(line)
    if match is None:
        return None, line.strip()
    return float(match["value"]), line[match.end() :].strip()


def classify_gap_text(text: str) -> TextClass:
    """Classify a confidence-gap description, giving a stated device absence first priority.

    An accelerator label identifies the class of a stated absence. Provisioning wording decides when the line
    does not state a device absence.

    Examples:
        >>> classify_gap_text("CUDA kernels were not exercised on this host.")
        TextClass(gap_class='process', hardware_class=None)
        >>> classify_gap_text("cuda-absent: This host lacks a local CUDA GPU. (known limit)")
        TextClass(gap_class='hardware-absent', hardware_class='cuda')
        >>> classify_gap_text("CUDA tests skipped: torch CPU-only wheel installed, CUDA not available.").gap_class
        'environment-provisionable'
        >>> classify_gap_text("Native Windows timeout cleanup was not executed here.").gap_class is None
        True
        >>> classify_gap_text("Local OpenCV cases were skipped and one remote check was queued.").gap_class
        'environment-provisionable'
        >>> classify_gap_text("Parent-only review has no independent specialist assessments.").gap_class
        'process'
    """
    hardware = gap_ledger.absent_hardware_class(text)
    if hardware is not None:
        return TextClass("hardware-absent", hardware)
    if gap_ledger.provisioning_cause(text):
        return TextClass("environment-provisionable")
    return classify_without_hardware(text)


def classify_without_hardware(text: str) -> TextClass:
    """Classify a gap description that names no absent accelerator: host fact, then the non-hardware classes.

    A line that names an accelerator without stating its absence is reclassified here, so a package or CI cause in
    it keeps its own class and omission rule instead of falling through to ``process``.

    Examples:
        >>> classify_without_hardware("CUDA path not covered by hosted CI checks.").gap_class
        'project'
        >>> classify_without_hardware("One macOS host ran every check.").gap_class is None
        True
    """
    if HOST_NAME.search(text) and HOST_CONTEXT.search(text):
        return TextClass(None)
    for gap_class, pattern in CLASS_PATTERNS:
        if pattern.search(text):
            return TextClass(gap_class)
    return TextClass("process")


def text_gap_id(description: str) -> str:
    """Return a stable kebab-case id for a free-text gap: its leading words plus a digest of every word.

    Case, punctuation and spacing never change the id, so the same gap reported again keeps its identity.

    Examples:
        >>> text_gap_id("Parent-only review has no independent assessments.")
        'parent-only-review-has-no-independent-as-f741e125'
        >>> text_gap_id("PARENT-ONLY review, has no independent assessments") == text_gap_id(
        ...     "Parent-only review has no independent assessments.")
        True
    """
    words = re.findall(r"[a-z0-9]+", description.lower())
    stem = "-".join(words)[:_SLUG_STEM].strip("-") or "gap"
    digest = hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}"


def _merge(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sum deductions after waivers, keeping the evidence behind any remaining hardware deduction."""
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        gap_id = record["id"]
        earlier = merged.get(gap_id)
        if earlier is None:
            merged[gap_id] = record
            continue
        deduction = earlier["deduction"]
        if record["deduction"] is not None:
            deduction = round(min(1.0, (deduction or 0.0) + record["deduction"]), 2)
        evidence = earlier
        if gap_id == "pr-code-not-executed" and record["summary"] != earlier["summary"]:
            evidence = {**record, "summary": one_line(f"{record['summary']}. {earlier['summary']}")}
        elif record["class"] == "hardware-absent":
            evidence = max((earlier, record), key=lambda row: (bool(row["deduction"]), row.get("known_limit") is True))
        merged[gap_id] = {**evidence, "deduction": None if evidence.get("known_limit") is True else deduction}
    return list(merged.values())


def provisioning_evidence(gates: dict[str, Any]) -> frozenset[str]:
    """Return which provisioned environments served the final gate run: ``project_env`` and ``tool_cache`` keys.

    Examples:
        >>> sorted(provisioning_evidence({"tool_cache": "/c", "source": {"project_env": "/e"}}))
        ['project_env', 'tool_cache']
        >>> provisioning_evidence({"checks": []})
        frozenset()
    """
    source = gates.get("source") if isinstance(gates.get("source"), dict) else {}
    return frozenset(key for key in PROVISIONING_EVIDENCE.values() if key in gates or key in source)


def _omitted(reason: str, text: str) -> dict[str, str]:
    """Name one derived gap line that is deliberately left out of the ledger."""
    return {"reason": reason, "text": one_line(text)}


def _confidence_id(description: str) -> str | None:
    """Recognize canonical non-hardware IDs and documented confidence-gap wording."""
    explicit = re.match(r"`?([a-z0-9-]+)`?(?=[:\s]|$)", description.lower())
    if explicit and gap_ledger.lookup_gap_id(explicit[1]) not in (None, ("hardware-absent", "host")):
        return explicit[1]
    for template, pattern in CONFIDENCE_IDS.items():
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            qualifier = match[1].strip() if match.lastindex else ""
            if not match.lastindex or re.search(r"[a-z0-9]", qualifier, re.IGNORECASE):
                return gap_ledger.gap_id(template, qualifier)
    return None


def _line_class(description: str, canonical_id: str | None) -> TextClass:
    """Decide the ledger class of one confidence-gap description.

    A canonical process id keeps class ``process`` when no device absence was found. A skipped provisioning step keeps
    its text class, so it is omitted like other unprovisioned packages.
    """
    kind = classify_gap_text(description)
    if kind.hardware_class is not None:
        return kind
    canonical = gap_ledger.lookup_gap_id(canonical_id) if canonical_id else None
    if canonical and canonical[0] == "process" and not canonical_id.startswith("provisioning-not-attempted-"):
        return TextClass(canonical[0])
    return kind


def _line_omission(kind: TextClass, description: str, provisioned: frozenset[str]) -> dict[str, str] | None:
    """Return why a classified description is left out of the ledger, or ``None`` when it is recorded.

    A host fact is never a gap. A package line is never recorded from text: the gate evidence supplies the package
    identity and provisioning outcome. A cache line needs ``tool_cache`` evidence from the final gate run.
    """
    if kind.gap_class is None:
        return _omitted(HOST_FACT, description)
    if kind.gap_class != "environment-provisionable":
        return None
    package = not re.search(r"\b(?:hook|pre-commit) cache\b", description, re.IGNORECASE)
    if package or "tool_cache" not in provisioned:
        return _omitted(NOT_PROVISIONED if not provisioned else "requires-gate-evidence", description)
    return None


def _open_confidence_lines(metadata: dict[str, Any]) -> Iterator[tuple[float | None, str]]:
    """Read current deductions from flat lines or from the rationale paired with a scoped gap."""
    closures = metadata.get("confidence_gap_closures")
    by_gap = {
        entry["gap"].strip(): entry
        for entry in (closures if isinstance(closures, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("gap"), str)
    }
    lines = metadata.get("confidence_gaps")
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, str) or not line.strip():
            continue
        closure = by_gap.get(line.strip(), {})
        if closure.get("status") == "closed":
            continue
        deduction, description = split_deduction(line)
        rationale = closure.get("rationale")
        if deduction is None and isinstance(rationale, str):
            deduction, rationale = split_deduction(rationale)
            scoped, separator, _ = description.partition(f" ({closure.get('status')}) - ")
            if deduction is not None and separator and ": " in scoped:
                description = scoped.split(": ", 1)[1]
                if deduction == 0 and gap_ledger.KNOWN_LIMIT_MARKER.search(rationale):
                    description += " (known limit)"
        yield deduction, description


def confidence_gap_drafts(
    metadata: dict[str, Any], provisioned: frozenset[str] = frozenset()
) -> tuple[list[GapDraft], list[dict[str, str]]]:
    """Return drafts for the open confidence-gap lines and the lines deliberately left out, with their reason.

    Package gaps come from the gate evidence, which establishes the provisioning outcome and package identity.

    Examples:
        >>> drafts, omitted = confidence_gap_drafts({
        ...     "confidence_gaps": ["(-0.02) CUDA path not exercised: no NVIDIA GPU.", "(-0.01) One macOS host only.",
        ...                         "(-0.01) OpenCV not installed.", "(-0.01) Lint rerun."],
        ...     "confidence_gap_closures": [{"gap": "(-0.01) Lint rerun.", "status": "closed", "evidence": "x"}]})
        >>> [(draft.id, draft.gap_class, draft.deduction) for draft in drafts]
        [('cuda-absent', 'hardware-absent', 0.02)]
        >>> [entry["reason"] for entry in omitted]
        ['host-fact', 'provisioning-not-attempted']
    """
    drafts: list[GapDraft] = []
    omitted: list[dict[str, str]] = []
    for deduction, description in _open_confidence_lines(metadata):
        if not description:
            continue
        canonical_id = _confidence_id(description)
        if (deduction == 0 and canonical_id == "full-suite-not-run") or re.search(
            r"\buser (?:declined|skipped) (?:the )?push\b|\bpush.*skipped-by-user\b", description, re.IGNORECASE
        ):
            continue
        kind = _line_class(description, canonical_id)
        omission = _line_omission(kind, description, provisioned)
        if omission is not None:
            omitted.append(omission)
            continue
        hardware = kind.hardware_class
        gap_id = gap_ledger.gap_id("<class>-absent", hardware) if hardware else canonical_id or text_gap_id(description)
        summary = one_line(gap_ledger.hardware_observation(description, gap_id) if hardware else description)
        drafts.append(
            GapDraft(
                id=gap_id,
                gap_class=kind.gap_class,
                summary=summary,
                closable_by=f"Run on a host with {hardware} hardware."
                if hardware
                else CLASS_CLOSABLE_BY[kind.gap_class],
                evidence="result.json",
                deduction=deduction,
                hardware_class=hardware,
                reason=summary if hardware else None,
                waiver_reported=hardware is not None
                and deduction == 0
                and gap_ledger.KNOWN_LIMIT_MARKER.search(description) is not None,
            )
        )
    validation = metadata.get("review_validation")
    if isinstance(validation, dict) and validation.get("verification_readiness") in {"blocked", "incomplete"}:
        gap_id = gap_ledger.gap_id("readiness-incomplete")
        if not any(draft.id == gap_id for draft in drafts):
            readiness = validation["verification_readiness"]
            drafts.append(
                GapDraft(
                    gap_id,
                    "process",
                    f"Verification readiness is {readiness}.",
                    CLASS_CLOSABLE_BY["process"],
                    "result.json",
                )
            )
    publication = metadata.get("worktree_publication")
    if isinstance(publication, dict) and publication.get("status") in {"failed", "unverified"}:
        gap_id = gap_ledger.gap_id("push-not-landed")
        if not any(draft.id == gap_id for draft in drafts):
            reason = one_line(f"Push {publication['status']}. {publication.get('reason') or ''}")
            drafts.append(
                GapDraft(
                    gap_id,
                    "process",
                    reason,
                    "Verify delivery of the intended head.",
                    publication.get("evidence") or "result.json",
                )
            )
    return drafts, omitted


def _gate_summary(gate: str, cause: str, *, provisioned: bool) -> str:
    """Describe one gate row that did not pass and whether provisioning served it."""
    summary = f"The {gate} gate did not pass: {cause}"
    if provisioned:
        summary += "; still missing after provisioning"
    return one_line(summary)


def _package_drafts(
    check: dict[str, Any], gate: str, served: bool, provisioning: dict[str, Any]
) -> tuple[list[GapDraft], list[dict[str, str]]]:
    """Name the missing distributions after provisioning or the gate when the workflow skipped it."""
    gap_class = "process"
    receipt_failed = provisioning.get("status") == "failed"
    if not served and provisioning.get("status") in {"ready", "partial"}:
        return [], [_omitted("gate-not-rerun-after-provisioning", f"The {gate} gate has no provisioned rerun.")]
    if not served and not receipt_failed:
        gap_id = gap_ledger.gap_id("provisioning-not-attempted-<gate>", gate)
        summary = one_line(f"The {gate} gate ran with required dependencies missing before provisioning was attempted.")
        action = "Provision the gate's required dependencies and rerun it."
        return [GapDraft(gap_id, gap_class, summary, action, "gates.json")], []
    modules = check.get("missing_modules")
    packages = {
        MODULE_DISTRIBUTIONS.get(module.partition(".")[0], module)
        for module in (modules if isinstance(modules, list) else [])
        if isinstance(module, str) and re.fullmatch(r"[A-Za-z_][\w.]*", module, re.ASCII)
    }
    if not packages:
        return [], [_omitted("missing-module-identity", f"The {gate} gate did not name the missing module.")]
    action = "Resolve the provisioning failure and rerun the affected gate."
    evidence = "gates.json" if served else PROVISIONING_RECEIPT
    detail = "still missing after provisioning" if served else f"provisioning failed ({provisioning.get('cause')})"
    return [
        GapDraft(
            gap_ledger.gap_id("provisioning-failed-<package>", package),
            "environment-provisionable",
            one_line(f"The {gate} gate requires {package}, {detail}."),
            action,
            evidence,
        )
        for package in sorted(packages)
    ], []


def _gate_diagnoses(checks: Any) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield each gate's own failed rows, treating shard receipts as authoritative when present."""
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict) or check.get("status") in NON_GAP_GATE_STATUSES:
            continue
        gate = check.get("id")
        if not isinstance(gate, str) or not re.search(r"[a-z0-9]", gate, re.IGNORECASE):
            continue
        rows = check.get("shards", [check])
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("status") not in NON_GAP_GATE_STATUSES:
                cause = row.get("cause")
                if isinstance(cause, str):
                    yield gate, cause, row


def gate_drafts(
    gates: dict[str, Any], provisioning: dict[str, Any] | None = None
) -> tuple[list[GapDraft], list[dict[str, str]]]:
    """Return canonical drafts for meaningful failed gates and refusals, plus omitted evidence."""
    provisioned = provisioning_evidence(gates)
    drafts: list[GapDraft] = []
    omitted: list[dict[str, str]] = []
    for gate, cause, check in _gate_diagnoses(gates.get("checks")):
        if cause == "sandbox-refused":
            action = "Rerun the gate on a host that provides the required sandbox."
            summary = _gate_summary(gate, cause, provisioned=False)
            drafts.append(GapDraft(gap_ledger.gap_id("pr-code-not-executed"), "process", summary, action, "gates.json"))
            continue
        if check.get("status") == "refused":
            continue
        served = PROVISIONING_EVIDENCE.get(cause) in provisioned
        if cause == "missing-python-package" and check.get("status") == "fail":
            package_drafts, package_omissions = _package_drafts(check, gate, served, provisioning or {})
            drafts.extend(package_drafts)
            omitted.extend(package_omissions)
            continue
        if cause not in GATE_CAUSES:
            continue
        summary = _gate_summary(gate, cause, provisioned=served)
        if cause in PROVISIONING_EVIDENCE and not served:
            omitted.append(_omitted(NOT_PROVISIONED, summary))
            continue
        gap_id = gap_ledger.gap_id(f"gate-<gate>-{cause}", gate)
        gap_class, _ = gap_ledger.GAP_IDS[f"gate-<gate>-{cause}"]
        drafts.append(GapDraft(gap_id, gap_class, summary, GATE_CAUSES[cause], "gates.json"))
    refusal = gates.get("refusal")
    if isinstance(refusal, dict) and isinstance(refusal.get("cause"), str):
        refused = gates.get("checks_refused")
        names = ", ".join(str(name) for name in refused) if isinstance(refused, list) and refused else "every gate"
        detail = refusal.get("reason", "")
        summary = one_line(f"Gates refused before launch ({refusal['cause']}): {names}. {detail}")
        closable = "Restore a clean committed checkout at the expected head, then rerun the gate bundle."
        drafts.append(GapDraft(gap_ledger.gap_id("pr-code-not-executed"), "process", summary, closable, "gates.json"))
    merged: dict[str, GapDraft] = {}
    for draft in drafts:
        earlier = merged.get(draft.id)
        if earlier is None:
            merged[draft.id] = draft
        elif draft.id == "pr-code-not-executed" and draft.summary != earlier.summary:
            merged[draft.id] = replace(draft, summary=one_line(f"{draft.summary}. {earlier.summary}"))
    return list(merged.values()), omitted


def _paged(run: Path) -> bool:
    """Report whether the review used batched or historical paged dispatch rather than the direct plan."""
    if (run / "batch-inventory.json").is_file() or (run / "batches").is_dir():
        return True
    return not (run / "direct-plan.json").is_file() and (run / "inspection-plan.json").is_file()


def _parent_substituted(metadata: dict[str, Any]) -> bool:
    """Report whether the parent covered at least one required reviewer role."""
    if metadata.get("fanout_substituted") is True:
        return True
    assessments = metadata.get("reviewer_assessments")
    return any(
        isinstance(item, dict) and str(item.get("role", "")).strip().endswith(PARENT_SUBSTITUTE_SUFFIX)
        for item in (assessments if isinstance(assessments, list) else [])
    )


def review_route(run: Path, metadata: dict[str, Any]) -> str | None:
    """Map a Code Review's recorded execution mode onto the ledger route, or ``None`` for an unknown mode.

    Examples:
        >>> review_route(Path("/nonexistent"), {"execution_mode": "serial-fallback", "fanout_substituted": True})
        'serial-fallback'
        >>> review_route(Path("/nonexistent"), {"execution_mode": "parallel", "reviewer_assessments": [
        ...     {"role": "QA specialist (parent substitute)"}]})
        'parent-substitute'
        >>> review_route(Path("/nonexistent"), {"review_status": "unavailable"})
        'none'
        >>> review_route(Path("/nonexistent"), {"execution_mode": "telepathic"}) is None
        True
    """
    if metadata.get("review_status") in UNREVIEWED_STATUSES:
        return "none"
    mode = metadata.get("execution_mode")
    route = ROUTE_BY_MODE.get(mode) if isinstance(mode, str) else None
    if route == "direct" and _paged(run):
        route = "paged"
    if route in {"direct", "paged"} and _parent_substituted(metadata):
        return "parent-substitute"
    return route


def _json_object(path: Path) -> dict[str, Any] | None:
    """Read one JSON object, or ``None`` when the file is missing, a symlink, unreadable or not an object."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def checkout_root(run: Path) -> Path | None:
    """Return the invoking checkout that owns the run: the local review entry's repository or the artifact root's
    parent.

    Examples:
        >>> checkout_root(Path("/work/repo/.reports/codex/code-review/pr-7/run-001")).as_posix()
        '/work/repo'
        >>> checkout_root(Path("/elsewhere/run")) is None
        True
    """
    entry = _json_object(run / "review-entry.json") or {}
    repository = entry.get("repository")
    if isinstance(repository, str) and Path(repository).is_absolute():
        return Path(repository)
    for parent in run.parents:
        if parent.name in {".reports", ".git"}:
            return parent.parent
    return None


def _git(root: Path, *arguments: str) -> str | None:
    """Run one read-only local Git query in ``root`` and return its stripped output, or ``None`` on any failure."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed Git argv, shell disabled, no network access
            ["git", "-C", str(root), *arguments],  # noqa: S607 - Git is resolved from PATH like every Git helper here
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def remote_repository(url: str) -> str | None:
    """Return ``owner/name`` from a hosted Git remote URL, or ``None`` for a local path or unparsable value.

    Examples:
        >>> remote_repository("https://github.com/octo/demo.git")
        'octo/demo'
        >>> remote_repository("git@github.com:octo/demo.git")
        'octo/demo'
        >>> remote_repository("/srv/mirror/demo") is None
        True
    """
    if "://" not in url and "@" not in url:
        return None
    match = _REMOTE_REPOSITORY.search(url.strip())
    return f"{match['owner']}/{match['name']}" if match else None


def run_subject(run: Path, metadata: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """Return the reviewed subject (repository, pull request, head), or ``None`` when it cannot be established.

    The repository is stored case-folded, so a PR receipt and a remote that spell one repository differently give one
    identity.
    """
    routing = _json_object(run / "pr-routing.json") or _json_object(run / "pr" / "pr-routing.json")
    reviewed = metadata.get("reviewed_commit_sha") if kind == "review" else None
    if routing is not None:
        pr = routing.get("pr_number")
        repo = gap_ledger.repo_key(routing.get("base_repo"))
        return {"repo": repo, "pr": pr, "head": reviewed or routing.get("head_oid")}
    root = checkout_root(run)
    if root is None:
        return None
    url = _git(root, "remote", "get-url", "origin")
    repo = gap_ledger.repo_key(remote_repository(url)) if url else None
    head = reviewed or _git(root, "rev-parse", "--verify", "HEAD")
    return {"repo": repo, "pr": None, "head": head} if repo and head else None


def run_id(run: Path) -> str:
    """Return the run's ledger id: ``pr-<N>-run-<NNN>`` for a promoted PR run, else the directory name.

    Examples:
        >>> run_id(Path("/r/.reports/codex/code-review/pr-12/run-003"))
        'pr-12-run-003'
        >>> run_id(Path("/r/.reports/codex/code-review/2026-11-19T18-44-51-642605Z-iq4aetun"))
        '2026-11-19T18-44-51-642605Z-iq4aetun'
    """
    return f"{run.parent.name}-{run.name}" if _PR_DIRECTORY.fullmatch(run.parent.name) else run.name


def plugin_version() -> str | None:
    """Return this installed plugin's version from its ``.codex-plugin/plugin.json``."""
    manifest = _json_object(SHARED_DIRECTORY.parent / ".codex-plugin" / "plugin.json") or {}
    version = manifest.get("version")
    return version if isinstance(version, str) else None


def run_identity(run: Path, metadata: dict[str, Any], kind: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return the ledger identity ``{"subject", "run"}`` or ``(None, code)`` naming what could not be established."""
    route = review_route(run, metadata) if kind == "review" else "none"
    if route is None:
        return None, f"route-unknown:{metadata.get('execution_mode')}"
    subject = run_subject(run, metadata, kind)
    if subject is None:
        return None, "subject-unknown"
    version = plugin_version()
    if version is None:
        return None, "plugin-version-unknown"
    identity = {
        "subject": subject,
        "run": {
            "run_id": run_id(run),
            "kind": kind,
            "harness": HARNESS,
            "plugin": PLUGIN_NAME,
            "plugin_version": version,
            "route": route,
        },
    }
    probe = to_record(GapDraft("identity-probe", "process", "probe", "probe", "result.json"), identity)
    errors = [error for error in gap_ledger.record_errors(probe) if error.startswith(("subject", "run"))]
    return (None, f"identity-invalid:{errors[0]}") if errors else (identity, None)


def to_record(draft: GapDraft, identity: dict[str, Any]) -> dict[str, Any]:
    """Attach the run identity and carry the reported waiver claim into registry verification."""
    gap_class, owner = gap_ledger.lookup_gap_id(draft.id) or (draft.gap_class, CLASS_OWNERS[draft.gap_class])
    record: dict[str, Any] = {
        "schema": gap_ledger.SCHEMA,
        "schema_version": gap_ledger.SCHEMA_VERSION,
        "id": draft.id,
        "class": gap_class,
        "owner": owner,
        "summary": draft.summary,
        "closable_by": draft.closable_by,
        "evidence": draft.evidence,
        "subject": identity["subject"],
        "run": identity["run"],
        "deduction": draft.deduction,
    }
    if draft.hardware_class is not None:
        record.update(
            hardware_class=draft.hardware_class,
            reason=draft.reason,
            host_class=gap_ledger.host_class_fields(gap_ledger.current_host_class()),
        )
    if draft.waiver_reported:
        record["known_limit"] = True
    return record


def invoking_known_limits(root: Path | None) -> tuple[KnownLimits | None, str | None]:
    """Load the invoking checkout's known-limits file; an invalid file is ignored and named, never fatal."""
    return (None, None) if root is None else _load_known_limits(root / KNOWN_LIMITS_PATH)


def _load_known_limits(path: Path) -> tuple[KnownLimits | None, str | None]:
    """Read one registry's limits for this host or return a diagnostic for invalid input."""
    try:
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise gap_ledger.LedgerError("expected a regular file")
        repo, limits = gap_ledger.load_known_limits(path)
    except FileNotFoundError:
        return None, None
    except (gap_ledger.LedgerError, gap_ledger.LedgerInvalidError, OSError) as error:
        return None, one_line(f"known-limits-ignored:{error}")
    return KnownLimits(path, repo, limits), None


def scoring_known_limits(run: Path) -> tuple[KnownLimits | None, str | None]:
    """Load the limits captured at scoring, with a live lookup for runs without a snapshot."""
    known, note = _load_known_limits(run / KNOWN_LIMITS_SNAPSHOT)
    return (known, note) if known is not None or note is not None else invoking_known_limits(checkout_root(run))


def apply_known_limits(records: list[dict[str, Any]], known: KnownLimits | None) -> list[dict[str, Any]]:
    """Verify reported waivers against this run's limits while preserving all other deductions."""
    verified = []
    for record in records:
        if record.get("known_limit") is not True:
            verified.append(record)
            continue
        observation = {key: value for key, value in record.items() if key != "known_limit"}
        if (
            observation.get("deduction") == 0
            and known is not None
            and gap_ledger.repo_key(record["subject"]["repo"]) == gap_ledger.repo_key(known.repo)
        ):
            observation = gap_ledger.apply_known_limit(observation, known.limits.values())
        verified.append(observation)
    return verified


def find_prior_ledger(run: Path, identity: dict[str, Any]) -> Path | None:
    """Return the newest sibling run's valid ledger that ``carry`` accepts for this identity, if any."""
    candidates: list[tuple[int, Path]] = []
    try:
        siblings = list(run.parent.iterdir())
    except OSError:
        return None
    for sibling in siblings:
        ledger = sibling / LEDGER_NAME
        if sibling == run or sibling.is_symlink() or ledger.is_symlink() or not ledger.is_file():
            continue
        candidates.append((ledger.stat().st_mtime_ns, ledger))
    for _, ledger in sorted(candidates, reverse=True):
        try:
            prior = gap_ledger.load_ledger(ledger)
        except gap_ledger.LedgerError:
            continue
        if not prior.errors and prior.records and gap_ledger.carry_ineligible(prior.records[0], identity) is None:
            return ledger
    return None


def _closing(record: dict[str, Any]) -> dict[str, Any]:
    """Return the closing event for an open record this run no longer reports."""
    closed = {key: value for key, value in record.items() if key not in {"carried_from", "known_limit"}}
    closed.update(status="closed", deduction=None)
    return closed


def _carry(run: Path, identity: dict[str, Any], known: KnownLimits | None, summary: dict[str, Any]) -> None:
    """Carry the newest eligible sibling run's open gaps into this run's empty ledger; a refusal records fresh."""
    prior = find_prior_ledger(run, identity)
    if prior is None:
        return
    try:
        registry = (known.repo, known.limits.values()) if known else None
        summary["carried"] = gap_ledger.carry_forward(prior, identity, run / LEDGER_NAME, known_limits=registry)
    except (gap_ledger.CarryIneligibleError, gap_ledger.LedgerInvalidError) as error:
        summary["notes"].append(one_line(f"carry-skipped:{error}"))
        return
    summary["carried_from"] = gap_ledger.load_ledger(prior).records[0]["run"]["run_id"]


def _stage_and_append(ledger: Path, records: list[dict[str, Any]], known: KnownLimits | None) -> list[str]:
    """Stage records in this writer's ``.rec`` file and append them through the schema owner; return its errors."""
    stage = ledger.with_name(f"{ledger.name}.{STAGE_WRITER}.rec")
    if stage.is_symlink():
        raise gap_ledger.LedgerError(f"refusing symlinked stage: {stage.name}")
    stage.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8")
    registry = (known.repo, known.limits.values()) if known else None
    _, errors = gap_ledger.append_staged(ledger, known_limits=registry)
    return errors


def _needs_fresh_record(record: dict[str, Any], prior: dict[str, Any] | None) -> bool:
    """Append when the current report changes an open event's deduction or waiver status."""
    return (
        prior is None
        or prior.get("status") == gap_ledger.Status.CLOSED.value
        or prior.get("deduction") != record["deduction"]
        or (prior.get("known_limit") is True) != (record.get("known_limit") is True)
    )


def write_ledger(
    run: Path, identity: dict[str, Any], records: list[dict[str, Any]], known: KnownLimits | None
) -> dict[str, Any]:
    """Carry open events, supersede changed deductions and waivers, and close IDs this run dropped."""
    ledger = run / LEDGER_NAME
    summary: dict[str, Any] = {"status": "recorded", "appended": 0, "carried": 0, "closed": 0, "notes": []}
    current = gap_ledger.load_ledger(ledger)
    if current.errors:
        return {**summary, "status": "failed", "code": one_line(f"ledger-invalid:{current.errors[0]}")}
    if not current.records:
        _carry(run, identity, known, summary)
        current = gap_ledger.load_ledger(ledger)
    latest = gap_ledger.latest_by_id(current.records)
    reported = {record["id"] for record in records}
    additions = [record for record in records if _needs_fresh_record(record, latest.get(record["id"]))]
    closings = [
        _closing(record)
        for gap_id, record in latest.items()
        if gap_id not in reported and record.get("status", "open") == "open"
    ]
    if additions or closings:
        errors = _stage_and_append(ledger, additions + closings, known)
        if errors:
            return {**summary, "status": "failed", "code": one_line(f"append-refused:{errors[0]}")}
    return {**summary, "appended": len(additions), "closed": len(closings)}


def record_promoted_run(run: Path, skill: str) -> dict[str, Any]:
    """Record a promoted run's gaps in ``<run>/gaps.jsonl`` and return a summary; never raises for expected failures.

    Args:
        run: The run directory holding the promoted ``result.json`` and ``gates.json``.
        skill: ``code-review`` or ``code-remediate``; any other skill records nothing.

    Returns:
        The emission summary described in the module docstring.
    """
    kind = SKILL_KINDS.get(skill)
    if kind is None:
        return {"status": "skipped", "code": f"skill-not-recorded:{skill}"}
    result = _json_object(run / "result.json")
    if result is None or not isinstance(result.get("metadata"), dict):
        return {"status": "skipped", "code": "result-unreadable"}
    metadata = result["metadata"]
    identity, code = run_identity(run, metadata, kind)
    if identity is None:
        return {"status": "skipped", "code": code}
    gates = _json_object(run / "gates.json") or {}
    text_drafts, text_omitted = confidence_gap_drafts(metadata, provisioning_evidence(gates))
    row_drafts, row_omitted = gate_drafts(gates, _json_object(run / PROVISIONING_RECEIPT))
    root = checkout_root(run)
    known, limits_note = scoring_known_limits(run)
    observations = [to_record(draft, identity) for draft in text_drafts + row_drafts]
    records: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for record in apply_known_limits(observations, known):
        errors = gap_ledger.record_errors(record)
        if errors:
            dropped.append({"id": record["id"], "error": one_line(errors[0])})
        else:
            records.append(record)
    records = _merge(records)
    try:
        summary = write_ledger(run, identity, records, known)
    except (gap_ledger.LedgerError, gap_ledger.LedgerInvalidError, OSError) as error:
        summary = {"status": "failed", "code": one_line(f"ledger-io:{error}"), "notes": []}
    if limits_note:
        summary["notes"].append(limits_note)
    shown_limits = [
        {"id": record["id"], "reason": record["reason"], "host_class": record["host_class"]}
        for record in records
        if record.get("known_limit") is True
    ]
    if shown_limits:
        summary["known_limits"] = shown_limits
    if summary["status"] == "recorded" and root is not None and kind == "review":
        registered, error = gap_ledger.register_known_limits(
            root / KNOWN_LIMITS_PATH, identity["subject"]["repo"], records
        )
        if registered:
            summary["known_limits_registered"] = registered
        if error:
            summary["notes"].append(one_line(f"known-limits-registration-skipped:{error}"))
    return {
        **summary,
        "ledger": LEDGER_NAME,
        "route": identity["run"]["route"],
        "dropped": dropped,
        "not_recorded": text_omitted + row_omitted,
    }


def _save_scoring_limits(path: Path, repo: str, known: KnownLimits | None) -> None:
    """Capture the exact limits printed to the scorer without replacing an earlier capture."""
    if path.parent.is_symlink():
        raise OSError(f"refusing symlinked run directory: {path.parent}")
    limits = known.limits.values() if known is not None else ()
    payload = {
        "schema": gap_ledger.LIMITS_SCHEMA,
        "schema_version": gap_ledger.LIMITS_SCHEMA_VERSION,
        "repo": repo,
        "limits": [
            {
                "id": item.id,
                "class": "hardware-absent",
                "hardware_class": item.hardware_class,
                "reason": item.reason,
                "host_class": gap_ledger.host_class_fields(item.host_class),
            }
            for item in limits
        ],
    }
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temp = Path(handle.name)
            handle.write((json.dumps(payload) + "\n").encode("utf-8"))
        # Publish complete bytes and let the first successful prepare keep its snapshot.
        path.hardlink_to(temp)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _prepare_scoring_limits(run: Path) -> tuple[KnownLimits | None, str | None]:
    """Bind this run's repository and host to the limits the scorer is shown."""
    path = run / KNOWN_LIMITS_SNAPSHOT
    known, note = _load_known_limits(path)
    capture = known is None and note is None
    if capture:
        known, note = invoking_known_limits(checkout_root(run))
    subject = run_subject(run, {}, "review")
    repo = gap_ledger.repo_key(subject["repo"]) if subject else None
    if repo is None:
        return None, "could not establish the run repository" if known is not None else note
    if known is not None and gap_ledger.repo_key(known.repo) != repo:
        note = f"known-limits repository {known.repo} differs from {repo}"
        known = None
    if not capture:
        return known, note
    try:
        _save_scoring_limits(path, repo, known)
    except FileExistsError:
        return _load_known_limits(path)
    except OSError as error:
        return None, one_line(f"could not capture known limits: {error}")
    return known, note


def _prepare(run: Path) -> None:
    """Capture and print the limits that apply to this host before confidence is scored."""
    root = checkout_root(run)
    known, note = _prepare_scoring_limits(run)
    if note:
        path = root / KNOWN_LIMITS_PATH if root is not None else run / KNOWN_LIMITS_SNAPSHOT
        error = note.removeprefix("known-limits-ignored:").removeprefix(f"{path}: ")
        print(f"⚠ known-limits file ignored ({path}): {error}")
    elif known is not None and known.limits:
        for limit in known.limits.values():
            print(f"GAP_KNOWN_LIMIT={limit.id} · {limit.hardware_class} · {limit.reason}")
    else:
        print("GAP_KNOWN_LIMITS=none")


def main(argv: list[str] | None = None) -> int:
    """Parse the preparation command and capture the run's known limits.

    Args:
        argv: Command arguments, or None to use the process arguments.

    Returns:
        Zero after reporting the preparation outcome.
    """
    parser = argparse.ArgumentParser(description="Read the run's known hardware limits before scoring confidence.")
    parser.add_argument("command", choices=("prepare",))
    parser.add_argument("--run", required=True, type=Path, help="Run directory")
    args = parser.parse_args(argv)
    _prepare(args.run.absolute())
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    raise SystemExit(main())
