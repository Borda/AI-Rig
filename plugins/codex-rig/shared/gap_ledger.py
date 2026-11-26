#!/usr/bin/env python
"""Validate, append and carry forward the per-run gap ledger (``gaps.jsonl``) of review, resolve and remediate runs.

## Purpose

    Give every review, resolve and remediate workflow one schema for the coverage gaps and limitations it records, so
    gaps can be counted across runs by pull request and gap identity instead of by run directory. A gap record names
    its class (process, environment-provisionable, hardware-absent, project, contribution, human-decision), the party
    that owns it, what would close it, the evidence it rests on, and the identity of the run that saw it: harness,
    plugin version and review route. This helper is the schema owner: it validates staged records before they land,
    appends them, carries the gaps of an earlier run on an identical head forward as ``carried_from`` events, and
    validates the per-repository known-limits file. A separate roll-up aggregates ledgers across runs.

## Scope

    Schema family ``gap-ledger``, version 1, one JSON object per line. Required keys: ``schema``, ``schema_version``,
    ``id`` (stable kebab-case gap identity, reused across runs), ``class``, ``summary``, ``owner``, ``closable_by``,
    ``evidence`` (relative POSIX path), ``subject`` (``repo``, ``pr`` or null, ``head``, optional ``ref``) and ``run``
    (``run_id``, ``kind``, ``harness``, ``plugin``, ``plugin_version``, ``route``). Optional keys: ``carried_from``
    (the earlier ``run_id`` the gap was carried from), ``deduction`` (the gap's ``(-0.NN)`` confidence share, so a
    known limit can be shown without being deducted again), ``status`` (``open`` or ``closed``; a closing event
    retracts an earlier record of the same id in the same run), and ``known_limit``. A ``hardware-absent`` record
    requires ``hardware_class``, the exact ``reason`` and ``host_class`` with the observing OS and architecture.
    Unknown keys are refused. Every record of one ledger carries the same ``run`` and ``subject``.

    The known-limits file (``known-limits`` version 1, one per repository, kept in ``.notes/known-limits.json``) lists
    absent hardware classes with their exact reason and required host class. A later observation for the same
    repository, ID, OS and architecture carries ``known_limit: true`` and no deduction. The rollup groups those
    observations by repository, ID and host class. A ``known_limit`` claim counts only when that file lists it:
    ``validate`` and ``append`` refuse a claim they cannot check (no ``--known-limits``, or a file of another
    repository), ``carry`` re-derives the flag from the given file instead of copying it, and the rollup counts an
    unlisted claim as an ordinary gap. A gap whose summary or reason names a package or build cause (a CPU-only wheel,
    an unprovisioned or uninstalled package) is never matched to or registered as a known limit. Repository names
    compare case-insensitively everywhere.

    The ledger only grows: corrections and carries are new event lines, readers take the last record per ``id``.

## Usage

    python gap_ledger.py validate LEDGER [--known-limits FILE]
    python gap_ledger.py append LEDGER [--known-limits FILE]
    python gap_ledger.py carry --from PRIOR_LEDGER --identity IDENTITY_JSON LEDGER [--known-limits FILE]
    python gap_ledger.py limits FILE
    python gap_ledger.py schema

    ``append`` reads the staged ``LEDGER.rec`` and any parallel ``LEDGER.<writer>.rec`` files (one JSON record per
    line), validates the extended ledger, appends them in file-name order and deletes the staged files. ``carry``
    reads the new run's identity (an object with ``subject`` and ``run``) and copies every open gap of the prior
    ledger only when the subject (repository, pull request, head) and the run kind, plugin and plugin version are
    identical; otherwise it writes nothing and names the difference. A carried known limit keeps its flag only while
    ``--known-limits`` of the same repository still lists it; otherwise it is left for the new run to record fresh.

## Outputs

    ``validate`` and ``limits`` print one line per error (``path:line: message``) or a single ``OK`` line. ``append``
    and ``carry`` print the number of records appended. ``schema`` prints the closed value sets and key lists as JSON.

## Failure

    Exit 0 on success; 1 when validation refuses the ledger, a staged record or the known-limits file (nothing is
    appended); 2 for an argument error; 3 when ``carry`` is not eligible (different head, pull request or plugin
    version); 4 when a file cannot be read or written, including a symlinked ledger or staged file.

## Used by

    Review, resolve and remediate workflows that record their coverage gaps, through the plugin's gap-ledger
    contract, and the cross-run gap roll-up where the plugin ships one, which imports this module's loaders. Every
    plugin that ships this file ships a byte-identical copy.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import platform
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

#: Schema family name stored in every ledger record.
SCHEMA = "gap-ledger"
#: Current ledger schema version; a new family starts at 1.
SCHEMA_VERSION = 1
#: Schema family name of the per-repository known-limits file.
LIMITS_SCHEMA = "known-limits"
#: Current known-limits schema version.
LIMITS_SCHEMA_VERSION = 1
#: Largest ledger, staged or known-limits file read in one piece.
MAX_FILE_BYTES = 10 * 1024 * 1024
#: Longest single-line text value (summary, closable_by, reason).
MAX_TEXT = 300

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_NOT_ELIGIBLE = 3
EXIT_IO = 4


class GapClass(str, Enum):
    """Who or what a gap belongs to, which decides whether the workflow should close it."""

    PROCESS = "process"
    ENVIRONMENT_PROVISIONABLE = "environment-provisionable"
    HARDWARE_ABSENT = "hardware-absent"
    PROJECT = "project"
    CONTRIBUTION = "contribution"
    HUMAN_DECISION = "human-decision"


class Owner(str, Enum):
    """Party responsible for closing a gap."""

    WORKFLOW = "workflow"
    HOST = "host"
    MAINTAINER = "maintainer"
    CONTRIBUTOR = "contributor"
    USER = "user"


class RunKind(str, Enum):
    """Workflow that wrote the ledger."""

    REVIEW = "review"
    RESOLVE = "resolve"
    REMEDIATE = "remediate"


class Harness(str, Enum):
    """Agent host that ran the workflow."""

    CLAUDE = "claude"
    CODEX = "codex"


class Route(str, Enum):
    """How the run's reviewers were dispatched; ``none`` for a run that dispatched no reviewer."""

    DIRECT = "direct"
    PAGED = "paged"
    SERIAL_FALLBACK = "serial-fallback"
    PARENT_SUBSTITUTE = "parent-substitute"
    NONE = "none"


class Status(str, Enum):
    """Whether a record states a gap or retracts an earlier record of the same id in the same run."""

    OPEN = "open"
    CLOSED = "closed"


class HardwareClass(str, Enum):
    """Hardware classes whose absence may stay a limit; packages, OS and Python versions never qualify."""

    CUDA = "cuda"
    ROCM = "rocm"
    TENSORRT = "tensorrt"
    XPU = "xpu"
    TPU = "tpu"
    NPU = "npu"
    MPS = "mps"
    MULTI_GPU = "multi-gpu"


_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_SLUG = 64
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_PLUGIN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Executable form of the ID table in the gap-ledger contract. Both installed emitters use this copy.
GAP_IDS = {
    "<class>-absent": ("hardware-absent", "host"),
    "provisioning-failed-<package>": ("environment-provisionable", "workflow"),
    "provisioning-not-attempted-<gate>": ("process", "workflow"),
    "pr-code-not-executed": ("process", "workflow"),
    "readiness-incomplete": ("process", "workflow"),
    "unreviewed-dimension-<unit>": ("process", "workflow"),
    "full-suite-not-run": ("process", "workflow"),
    "push-not-landed": ("process", "workflow"),
    "codex-co-review-<reason>": ("process", "workflow"),
    "gate-<gate>-hook-cache-unwritable": ("environment-provisionable", "workflow"),
    "gate-<gate>-network-unavailable": ("environment-provisionable", "workflow"),
    "gate-<gate>-loopback-bind-blocked": ("process", "workflow"),
    "gate-<gate>-timeout": ("process", "workflow"),
    "gate-<gate>-missing-command": ("process", "workflow"),
    "gate-<gate>-import-origin-unproven": ("process", "workflow"),
    "implementation-challenge-not-run": ("process", "workflow"),
}
_ID_SLOT = re.compile(r"<[a-z]+>")
_ID_PATTERNS = {template: re.compile(_ID_SLOT.sub("([a-z0-9]+(?:-[a-z0-9]+)*)", template)) for template in GAP_IDS}

_SOFTWARE_NOUN = (
    r"(?:toolkits?|drivers?|runtimes?|librar(?:y|ies)|wheels?|packages?|builds?|images?|pytorch|torch"
    r"|binary|binaries|backends?|containers?|stacks?|dependenc(?:y|ies)|modules?)\b"
)
_SOFTWARE_STATUS = r"missing|not (?:installed|loaded|found)|unavailable|not available|failed"

#: Wording that routes a gap to provisioning when it states no device absence.
PROVISIONING_CAUSE = re.compile(
    r"\bcpu[- ]only\b|\bprovision"
    r"|\b(?:cpu|non-cuda)[- ](?:build|wheel|package|index|variant)s?\b|\+cpu\b"
    rf"|\b{_SOFTWARE_NOUN}\s+(?:(?:is|are|was|were)\s+)?(?:{_SOFTWARE_STATUS})\b"
    rf"|\b(?:missing|wrong|mismatched|incompatible)\s+(?:[\w.+-]+\s+){{0,3}}(?:{_SOFTWARE_NOUN}|versions?\b)"
    r"|\b(?:not|never) (?:installed|importable)\b|\bfailed to install\b|\binstall(?:ation)? failed\b"
    r"|\bmodulenotfounderror\b|\bimporterror\b|\bno module named\b"
    r"|\b(?:built|compiled) without\b"
    r"|\b(?:cuda|rocm|gpu|xpu|mps)[- ]enabled (?:build|package|install(?:ation)?)\b",
    re.IGNORECASE,
)
#: Recognize accelerator classes in device descriptions and surrounding gap text, most specific first.
HARDWARE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("multi-gpu", re.compile(r"\bmulti[- ]?gpus?\b|\bmultiple gpus\b", re.IGNORECASE)),
    ("tensorrt", re.compile(r"\btensorrt\b", re.IGNORECASE)),
    ("cuda", re.compile(r"\bcuda\b|\bnvidia\b", re.IGNORECASE)),
    ("rocm", re.compile(r"\brocm\b|\bamd gpus?\b", re.IGNORECASE)),
    ("xpu", re.compile(r"\bxpus?\b", re.IGNORECASE)),
    ("tpu", re.compile(r"\btpus?\b", re.IGNORECASE)),
    ("npu", re.compile(r"\bnpus?\b", re.IGNORECASE)),
    ("mps", re.compile(r"\bmps\b", re.IGNORECASE)),
)
_HOST_DESCRIPTION = (
    r"(?:(?:this|the|these|those|our|a)\s+)?"
    r"(?:(?:local|remote|test|ci|hosted|linux|macos|windows|x86_64|arm64)\s+){0,3}"
)
_DEVICE_HOST = rf"{_HOST_DESCRIPTION}(?:hosts?|runners?|machines?)"
_REPORT_HOST = (
    rf"{_HOST_DESCRIPTION}(?:hosts?|runners?|machines?|environments?|vms?|nodes?|workers?|servers?|workstations?|ci)"
)
_DEVICE_NOUN = r"(?:multiple gpus|multi[- ]?gpus?|gpus?|accelerators?|hardware|devices?|cards?|tpus?|npus?|xpus?)\b"
_DEVICE_BRAND = r"nvidia|amd|cuda|rocm|intel|apple|mps|tensorrt"
_REPORT_MODIFIER = r"[\w-]+(?:able|ible|ed|ing|al|ic|ive|ate|ete|ite|ary|less|ous|ly)"
_ABSENCE_LOCATION = rf"(?:\s+here\b|\s+(?:on|in)\s+{_DEVICE_HOST}\b)?"
_SINGLE_QUANTITY = r"(?:(?:only|just)\s+)?(?:1(?!\.\d)|one|(?:a\s+)?single)"
_SINGLE_GPU = rf"{_SINGLE_QUANTITY}\s+(?:(?:{_DEVICE_BRAND})\s+)?gpus?\b"
_CAPACITY_CONTEXT = (
    rf"(?:\s+(?:(?:is|are|was|were)\s+)?(?:available|present|attached|installed|provisioned)\b"
    rf"|\s+{_DEVICE_HOST}\b|\s+(?:here\b|(?:on|in|at)\s+{_REPORT_HOST}\b)"
    r"|\s*(?=$|[.;)]|(?:and|but)\s+(?:the|this|that)\b|so\b))"
)
_DEVICE_CAPACITY = re.compile(_CAPACITY_CONTEXT, re.IGNORECASE)
_SECOND_GPU = re.compile(r"\b(?:a\s+)?second\s+$", re.IGNORECASE)
_OTHER_QUANTITY = r"\d+(?:\.\d+)?|two|three|four|five|six|seven|eight|nine|ten|\w+teen|\w+ty|several|some|a few"
_ORDINAL_QUANTITY = r"first|second|third|[a-z]{3,}th|\d+(?:st|nd|rd|th)"  # codespell:ignore nd
_SUBSET_QUANTITY = re.compile(
    rf"\b(?:not\s+all|{_ORDINAL_QUANTITY}|(?:{_SINGLE_QUANTITY}|{_OTHER_QUANTITY})"
    rf"(?:\s+of(?:\s+(?:the|our|their|these|those))?(?:\s+(?:{_OTHER_QUANTITY}))?)?)"
    rf"(?:\s+{_REPORT_MODIFIER})*\s+$",
    re.IGNORECASE,
)

# Bind an absence cue to a device phrase anywhere in the line. Consume an explicit device noun before
# considering a bare accelerator keyword, which can also name a software backend.
_HARDWARE_KEYWORD = "|".join(pattern.pattern for _, pattern in HARDWARE_PATTERNS)
_CAPABILITY_SUFFIX = r"[- ](?:capable|compatible|enabled|ready)"
_REPORTED_DEVICE = re.compile(
    rf"\b(?:(?:(?:{_HARDWARE_KEYWORD}|amd|intel|apple)(?:{_CAPABILITY_SUFFIX})?[\s-]+)*"
    rf"(?P<noun>{_DEVICE_NOUN})|(?:{_HARDWARE_KEYWORD}))",
    re.IGNORECASE,
)
_ABSENCE_PREDICATE = (
    r"absent|missing|unavailable|not\s+(?:available|present|attached|detected|found|installed|provisioned)"
)
_ABSENCE_BEFORE = re.compile(
    rf"(?<![.\d+-])\b(?:(?P<single>{_SINGLE_QUANTITY})"
    rf"|(?P<zero>zero|0|none(?:\s+of)?|not\s+(?:(?:one|any)(?:\s+of)?|a\s+single))"
    rf"|no|without|lacks?|lacking|{_ABSENCE_PREDICATE}|(?:does|do|did)\s+not\s+have)"
    rf"(?:[\s:]+(?:a|an|the|any|{_REPORT_MODIFIER}))*(?:\s+(?P<second>second))?[\s:]+$",
    re.IGNORECASE,
)
_ABSENCE_AFTER = re.compile(
    rf"[\s:=–—]*(?:(?:is|are|was|were|(?:has|have)\s+been|\w+ly)\s+)*(?:{_ABSENCE_PREDICATE}|(?P<none>none))\b"
    r"|\s*[:=]\s*(?P<zero>no|zero|0)\b(?!\.\d)",
    re.IGNORECASE,
)
_SOFTWARE_TAIL = re.compile(
    rf"(?:{_CAPABILITY_SUFFIX})?[\s-]+(?:(?:{_REPORT_MODIFIER}|v?\d+(?:\.\d+)*)[\s-]+)*{_SOFTWARE_NOUN}",
    re.IGNORECASE,
)
_NON_DEVICE_TAIL = re.compile(
    r"[\s-]+(?:tests?|checks?|kernels?|benchmarks?|paths?|coverage|support|regressions?|required|needed)\b",
    re.IGNORECASE,
)
_SOFTWARE_REPORTER = re.compile(
    rf"\b{_SOFTWARE_NOUN}\s+(?:reports?|shows?|returns?|lists?|detects?|finds?|sees?|has|have|built|compiled)\s*$",
    re.IGNORECASE,
)
_SOFTWARE_IN_TEXT = re.compile(rf"\b{_SOFTWARE_NOUN}", re.IGNORECASE)
_SOFTWARE_HEAD = re.compile(rf"\b{_SOFTWARE_NOUN}\s*$", re.IGNORECASE)
_CPU_BUILD = re.compile(r"\b(?:built|compiled) for (?:the )?(?:host )?cpu\b", re.IGNORECASE)
_DETECTION_CUE = re.compile(r"\b(?:detected|found)\b", re.IGNORECASE)
_CAUSE_RELATION = r"because(?:\s+of)?|due\s+to|owing\s+to|since|as|after|following"
_CAUSE_NOTE = re.compile(rf"\s*(?:{_CAUSE_RELATION})\b", re.IGNORECASE)
_REPORT_CONSEQUENCE = re.compile(
    rf"(?P<source>[^;.!?]+?)[,;]?\s+(?:so|therefore|thus|as a result),?\s+"
    rf"(?:(?:{_REPORT_HOST}\s+)?(?:has|have)|there\s+(?:is|are))?\s*$",
    re.IGNORECASE,
)
_REPORT_PREDICATE = re.compile(
    r"\s+(?:(?:is|are|was|were)\s+)?(?:available|present|attached|installed|provisioned)\b", re.IGNORECASE
)
_REPORT_QUALIFIER = re.compile(
    rf"(?:\s+(?:here\b|(?:on|in|at)\s+{_REPORT_HOST}\b))?"
    r"\s*(?:\((?P<note>[^)]*)\)|(?:(?P<visibility>visible|recognized|detected|found)\s+)?"
    rf"(?:(?P<relation>in|by|with|under|from)\s+|(?P<cause>(?:,\s*)?(?:{_CAUSE_RELATION})\s+|[:–—]\s*|-\s+))"
    r"(?P<source>(?:[^,;.!?)]|\.(?=\w))+))",
    re.IGNORECASE,
)
_REPORT_LOCATION = re.compile(r"\s+(?:here|on|in|at)\b", re.IGNORECASE)

# A waiver requires physical absence. Keep visibility and software setup out of this grammar.
_PHYSICAL_DEVICE = rf"(?:(?:local|physical|{_DEVICE_BRAND})\s+){{0,3}}{_DEVICE_NOUN}"
_PHYSICAL_ABSENCE_CLAUSE = (
    rf"(?:(?:{_DEVICE_HOST}\s+(?:(?:has|have)\s+no|lacks?|without)|host\s+has\s+no"
    rf"|(?:there\s+(?:is|are)\s+)?no|missing|lacks?)\s+(?:an?\s+)?"
    rf"(?P<device>{_PHYSICAL_DEVICE})(?:\s+(?:(?:is|are|was|were)\s+)?(?:attached|present)\b)?"
    rf"|(?P<predicate_device>{_PHYSICAL_DEVICE})\s+(?:(?:is|are|was|were)\s+)?"
    rf"(?:absent|missing|not\s+(?:present|attached))\b|{_SINGLE_GPU}){_ABSENCE_LOCATION}"
)
_HARDWARE_LABEL = "|".join(item.value for item in HardwareClass)
_TEST_SCOPE = rf"(?:(?:{_HARDWARE_LABEL}|accelerator)\s+)?(?:tests?|checks?|kernels?|paths?|coverage)"
_TEST_STATUS = r"\s+(?:(?:was|were)\s+)?(?:skipped|untested|not (?:run|tested|executed|exercised))"
#: A permanent waiver requires the entire reason to describe the device and optionally its test consequence.
_KNOWN_LIMIT_REASON = re.compile(
    rf"\s*(?:(?:(?P<label>{_HARDWARE_LABEL})-absent|{_DEVICE_HOST}|{_TEST_SCOPE}(?:{_TEST_STATUS})?)\s*:\s*)?"
    rf"{_PHYSICAL_ABSENCE_CLAUSE}(?:,\s+so\s+(?:the\s+)?{_TEST_SCOPE}"
    rf"\s+(?:cannot|could not|did not)\s+(?:run|execute|start|be exercised))?"
    rf"\.?(?:\s*\(known limit\))?\s*",
    re.IGNORECASE,
)
# A summary may describe test coverage or evidence without adding an explanation for the absence.
_NEUTRAL_LIMIT_SUMMARY = re.compile(
    rf"\s*(?:{_TEST_SCOPE}{_TEST_STATUS}|evidence\s+(?:was\s+)?unavailable"
    rf"|(?P<label>{_HARDWARE_LABEL})-absent\s+observed)\.?\s*",
    re.IGNORECASE,
)
_LIMIT_PREFIX = re.compile(r"^known limit\s*:\s*", re.IGNORECASE)
_REPRODUCIBILITY_PREFIX = re.compile(r"^not reproducible on this host\s*:\s*(?:requires\s+[^;]+;\s*)?", re.IGNORECASE)
KNOWN_LIMIT_MARKER = re.compile(r"\s+\(known limit\)[.!?]?\s*$", re.IGNORECASE)
_INVENTORY_NOTE = re.compile(
    r"\s+\((?:confirmed by (?:the )?)?(?:(?:accelerator|device|gpu|hardware|host)\s+)?inventory"
    r"(?:\s+(?:probe|check))?\)[.!?]?\s*$",
    re.IGNORECASE,
)
#: A lock directory older than this many seconds belongs to a writer that died; registration removes it once.
STALE_LOCK_SECONDS = 10 * 60


def provisioning_cause(text: object) -> bool:
    """Report whether a gap text names a missing, unprovisioned or wrong package or build as its cause.

    Examples:
        >>> provisioning_cause("CUDA tests skipped: torch CPU-only wheel installed, CUDA not available.")
        True
        >>> provisioning_cause("This host has no NVIDIA GPU.")
        False
        >>> provisioning_cause("TensorRT engine build untested here.")
        False
    """
    return isinstance(text, str) and PROVISIONING_CAUSE.search(text) is not None


def hardware_observation(text: str, gap_id: str | None = None) -> str:
    """Unwrap report labels and an inventory citation without dropping qualifiers on the device statement."""
    observation = _LIMIT_PREFIX.sub("", text.strip())
    if _REPRODUCIBILITY_PREFIX.match(observation):
        observation = _REPRODUCIBILITY_PREFIX.sub("", observation)
        observation = _INVENTORY_NOTE.sub("", observation)
    heading, separator, reason = observation.partition(":")
    if gap_id and separator and heading.strip("` ").casefold() == gap_id:
        observation = reason.strip()
    return KNOWN_LIMIT_MARKER.sub("", observation)


def device_absence(text: object) -> bool:
    """Report whether text contains an independent assertion that an accelerator device is absent.

    Examples:
        >>> device_absence("This host has no NVIDIA GPU.")
        True
        >>> device_absence("torch 2.3.0+cpu installed, CUDA not available")
        False
        >>> device_absence("No GPU tests executed on this host.")
        False
        >>> device_absence("NVIDIA GPU not detected by the shipped runtime.")
        False
    """
    return isinstance(text, str) and any(True for _ in _device_reports(text))


def _device_reports(text: str) -> Iterator[tuple[str | None, bool]]:
    """Yield each independent absence's named class and whether it gives a generic GPU count."""
    for device in _REPORTED_DEVICE.finditer(text):
        before = _ABSENCE_BEFORE.search(text, 0, device.start())
        after = _ABSENCE_AFTER.match(text, device.end())
        if (before is None) == (after is None):
            continue
        if _SOFTWARE_TAIL.match(text, device.end()) or _NON_DEVICE_TAIL.match(text, device.end()):
            continue
        start = before.start() if before else device.start()
        capacity = _gpu_capacity(text, device, before, after)
        if capacity is not None:
            if capacity > 0 and not _software_scopes_report(text, start, capacity):
                yield "multi-gpu", False
            continue
        if not before and _SUBSET_QUANTITY.search(text, 0, device.start()):
            continue
        end = after.end() if after else device.end()
        if _requires_inventory_context(before, after) and not _DEVICE_CAPACITY.match(text, end):
            continue
        if _software_scopes_report(text, start, end):
            continue
        if device["noun"] is None and provisioning_cause(text) and not _REPORT_LOCATION.match(text, end):
            continue
        hardware = next((name for name, pattern in HARDWARE_PATTERNS if pattern.search(device[0])), None)
        yield hardware, device[0].casefold() in {"gpu", "gpus"}


def _requires_inventory_context(before: re.Match[str] | None, after: re.Match[str] | None) -> bool:
    """Require an inventory assertion when a zero quantity could describe test utilization instead."""
    return bool((before and before["zero"]) or (after and (after["zero"] or after["none"])))


def _gpu_capacity(
    text: str, device: re.Match[str], before: re.Match[str] | None, after: re.Match[str] | None
) -> int | None:
    """Return the end of a capacity assertion, zero for an unsupported count, or None for class absence."""
    single = before and before["single"]
    second = (before and before["second"]) or (after and _SECOND_GPU.search(text, 0, device.start()))
    if not (single or second):
        return None
    if (device["noun"] or "").casefold() not in {"gpu", "gpus"} or (single and after):
        return 0
    if after:
        return after.end()
    capacity = _DEVICE_CAPACITY.match(text, device.end())
    return capacity.end() if capacity else 0


def _software_scopes_report(text: str, start: int, end: int) -> bool:
    """Recognize software as the source of a device report or the qualifier immediately attached to it."""
    if _SOFTWARE_REPORTER.search(text, 0, start):
        return True
    consequence = _REPORT_CONSEQUENCE.search(text, 0, start)
    if consequence is not None and provisioning_cause(consequence["source"]):
        return True
    if predicate := _REPORT_PREDICATE.match(text, end):
        end = predicate.end()
    qualifier = _REPORT_QUALIFIER.match(text, end)
    if qualifier is None:
        return False
    detection = _DETECTION_CUE.search(text, start, end) is not None
    if note := qualifier["note"]:
        if provisioning_cause(note) or (_CAUSE_NOTE.match(note) and _SOFTWARE_IN_TEXT.search(note)):
            return True
        return _SOFTWARE_IN_TEXT.search(note) is not None and (detection or _CPU_BUILD.search(note) is not None)
    source = qualifier["source"] or ""
    if qualifier["cause"]:
        return provisioning_cause(source) or _SOFTWARE_IN_TEXT.search(source) is not None
    if detection or qualifier["visibility"]:
        return _SOFTWARE_IN_TEXT.search(source) is not None
    # A cause can coexist with an independent absence. A reporting context must name software itself.
    return qualifier["relation"] is not None and _SOFTWARE_HEAD.search(source) is not None


def _device_classes(clause: re.Match[str]) -> tuple[str, ...]:
    """Find classes named by the device noun, with a single GPU indicating a multi-GPU limit."""
    device = clause["device"] or clause["predicate_device"]
    if device is None:
        return ("multi-gpu",)
    return tuple(hardware for hardware, pattern in HARDWARE_PATTERNS if pattern.search(device))


def absent_hardware_class(text: str) -> str | None:
    """Classify a stated absence using a named device, a canonical label, or a generic device's test context."""
    text = hardware_observation(text)
    reports = tuple(_device_reports(text))
    if not reports:
        return None
    if named := next((hardware for hardware, _ in reports if hardware is not None), None):
        return named
    heading, _, _ = text.partition(":")
    label = heading.strip("` ").casefold()
    if lookup_gap_id(label) == ("hardware-absent", "host"):
        return label.removesuffix("-absent")
    context = " ".join(
        device[0] for device in _REPORTED_DEVICE.finditer(text) if not _SOFTWARE_TAIL.match(text, device.end())
    )
    # A generic count proves the host cannot run multi-GPU work without implying a vendor.
    fallback = "multi-gpu" if any(generic_count for _, generic_count in reports) else None
    return next((hardware for hardware, pattern in HARDWARE_PATTERNS if pattern.search(context)), fallback)


def absent_device_clause(reason: object) -> re.Match[str] | None:
    """Parse a device assertion whose entire reason is eligible for class checking."""
    return _KNOWN_LIMIT_REASON.fullmatch(hardware_observation(reason)) if isinstance(reason, str) else None


def repo_key(repo: object) -> str | None:
    """Return the case-insensitive identity of an ``owner/name`` repository, or ``None`` for a non-string value.

    Hosted repository names are case-insensitive, so ``Octo/Demo`` and ``octo/demo`` are one repository. Every
    comparison across runs, registries and emitters goes through this function. Records inside one ledger share one
    subject written by one emitter, so ``consistency_errors`` compares them exactly.

    Examples:
        >>> repo_key("Octo/Demo") == repo_key("octo/demo") == "octo/demo"
        True
        >>> repo_key(None) is None
        True
    """
    return repo.casefold() if isinstance(repo, str) else None


def gap_id(template: str, value: str = "") -> str:
    """Build a canonical ID from a table template and its optional qualifier."""
    if template not in GAP_IDS:
        raise ValueError(f"unknown gap id template: {template}")
    # Keep the whole template, including the suffix, within the ledger's ID bound.
    room = _MAX_SLUG - len(_ID_SLOT.sub("", template))
    qualifier = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:room].rstrip("-")
    result = _ID_SLOT.sub(qualifier, template)
    if lookup_gap_id(result) is None:
        raise ValueError(f"invalid qualifier for {template}: {value!r}")
    return result


def lookup_gap_id(value: str) -> tuple[str, str] | None:
    """Return the canonical class and owner for an ID, or None for an unlisted ID."""
    if len(value) > _MAX_SLUG:
        return None
    for template, pattern in _ID_PATTERNS.items():
        match = pattern.fullmatch(value)
        if match and (template != "<class>-absent" or match[1] in HardwareClass._value2member_map_):
            return GAP_IDS[template]
    return None


_REQUIRED = frozenset({"schema", "schema_version", "id", "class", "summary", "owner", "closable_by", "evidence"})
_REQUIRED |= frozenset({"subject", "run"})
_OPTIONAL = frozenset({"carried_from", "deduction", "status", "known_limit", "hardware_class", "reason", "host_class"})
_SUBJECT_KEYS = frozenset({"repo", "pr", "head", "ref"})
_RUN_KEYS = frozenset({"run_id", "kind", "harness", "plugin", "plugin_version", "route"})
_LIMIT_KEYS = frozenset({"id", "class", "hardware_class", "reason", "host_class"})
_LIMITS_FILE_KEYS = frozenset({"schema", "schema_version", "repo", "limits"})


class LedgerError(Exception):
    """A ledger, staged record, identity or known-limits file that cannot be read safely (exit 4)."""


class LedgerInvalidError(Exception):
    """Input that was read but violates the schema, so nothing is written (exit 1)."""


class CarryIneligibleError(Exception):
    """The prior run differs in head, pull request, kind, plugin or plugin version, so nothing is carried (exit 3)."""


@dataclass(frozen=True)
class KnownLimit:
    """One absent hardware class listed for a repository and host class."""

    id: str
    hardware_class: str
    reason: str
    host_class: tuple[str, str]


def current_host_class() -> tuple[str, str]:
    """Return the platform's OS and architecture with common spelling aliases normalized."""
    system, machine = platform.system().casefold(), platform.machine().casefold()
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return system, architecture


def _host_class(value: object) -> tuple[str, str] | None:
    """Read the OS and architecture of a serialized observation."""
    if not isinstance(value, dict) or set(value) != {"os", "architecture"}:
        return None
    system, machine = value["os"], value["architecture"]
    if all(isinstance(item, str) and re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", item) for item in (system, machine)):
        return system, machine
    return None


def host_class_fields(host: tuple[str, str]) -> dict[str, str]:
    """Serialize an OS and architecture for a hardware gap or known limit."""
    return {"os": host[0], "architecture": host[1]}


@dataclass
class Ledger:
    """Records of one ledger file with the errors validation found, keyed by 1-based line number."""

    path: Path
    records: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _one_line_text(value: object, name: str) -> list[str]:
    """Return errors for a value that must be a non-empty single-line string of bounded length.

    Examples:
        >>> _one_line_text("ok", "summary")
        []
        >>> _one_line_text("two\\nlines", "summary")
        ['summary must be one line']
    """
    if not isinstance(value, str) or not value.strip():
        return [f"{name} must be a non-empty string"]
    if "\n" in value or "\r" in value:
        return [f"{name} must be one line"]
    if len(value) > MAX_TEXT:
        return [f"{name} longer than {MAX_TEXT} characters"]
    return []


def _enum_value(value: object, kind: type[Enum], name: str) -> list[str]:
    """Return an error when ``value`` is not one of the closed set's string values.

    Examples:
        >>> _enum_value("process", GapClass, "class")
        []
        >>> _enum_value("environment-inherent", GapClass, "class")[0].endswith("got 'environment-inherent'")
        True
    """
    allowed = [member.value for member in kind]
    if not isinstance(value, str) or value not in allowed:
        return [f"{name} must be one of {', '.join(allowed)}: got {value!r}"]
    return []


def _slug(value: object, name: str) -> list[str]:
    """Return an error unless ``value`` is a lowercase kebab-case identifier of bounded length.

    Examples:
        >>> _slug("codemap-index-stale", "id")
        []
        >>> _slug("Codemap Stale", "id")
        ["id must be a kebab-case slug: got 'Codemap Stale'"]
    """
    if not isinstance(value, str) or not _SLUG.match(value) or len(value) > _MAX_SLUG:
        return [f"{name} must be a kebab-case slug: got {value!r}"]
    return []


def _relative_path(value: object, name: str) -> list[str]:
    """Return an error unless ``value`` is a relative POSIX path that stays inside its base directory.

    Examples:
        >>> _relative_path("checks/tests.stdout.txt", "evidence")
        []
        >>> _relative_path("/abs/x", "evidence")
        ['evidence must be a relative POSIX path inside the run or repository: got /abs/x']
        >>> _relative_path("C:\\\\x", "evidence")[0].startswith("evidence must")
        True
    """
    message = [f"{name} must be a relative POSIX path inside the run or repository: got {value}"]
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT or "\\" in value:
        return message
    posix = PurePosixPath(value)
    if posix.is_absolute() or PureWindowsPath(value).drive or value.startswith("~") or ".." in posix.parts:
        return message
    return []


def _deduction(value: object) -> list[str]:
    """Return an error unless the deduction is null or a two-decimal number between 0 and 1.

    Examples:
        >>> _deduction(0.05)
        []
        >>> _deduction(0.055)
        ['deduction must be null or a number from 0 to 1 with at most two decimals: got 0.055']
    """
    if value is None:
        return []
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
        if round(float(value), 2) == float(value):
            return []
    return [f"deduction must be null or a number from 0 to 1 with at most two decimals: got {value}"]


def _subject_errors(subject: object) -> list[str]:
    """Return errors for the reviewed subject: repository, pull request (or null) and head commit.

    Examples:
        >>> _subject_errors({"repo": "octo/demo", "pr": 12, "head": "a1b2c3d"})
        []
        >>> _subject_errors({"repo": "octo/demo", "pr": 0, "head": "a1b2c3d"})
        ['subject.pr must be a positive integer or null']
    """
    if not isinstance(subject, dict):
        return ["subject must be an object"]
    errors = [f"subject has unknown key {key!r}" for key in sorted(set(subject) - _SUBJECT_KEYS)]
    errors += [f"subject.{key} is required" for key in ("repo", "pr", "head") if key not in subject]
    repo, pr, head = subject.get("repo"), subject.get("pr"), subject.get("head")
    if "repo" in subject and (not isinstance(repo, str) or not _REPO.match(repo)):
        errors.append(f"subject.repo must be owner/name: got {repo!r}")
    if "pr" in subject and pr is not None and (isinstance(pr, bool) or not isinstance(pr, int) or pr < 1):
        errors.append("subject.pr must be a positive integer or null")
    if "head" in subject and (not isinstance(head, str) or not _SHA.match(head)):
        errors.append(f"subject.head must be a lowercase hex commit id: got {head!r}")
    if subject.get("ref") is not None:
        errors += [f"subject.{error}" for error in _one_line_text(subject["ref"], "ref")]
    return errors


def _run_errors(run: object) -> list[str]:
    """Return errors for the run identity: id, kind, harness, plugin, plugin version and review route.

    Examples:
        >>> _run_errors({"run_id": "r1", "kind": "review", "harness": "claude", "plugin": "oss",
        ...              "plugin_version": "0.69.0", "route": "direct"})
        []
    """
    if not isinstance(run, dict):
        return ["run must be an object"]
    errors = [f"run has unknown key {key!r}" for key in sorted(set(run) - _RUN_KEYS)]
    errors += [f"run.{key} is required" for key in sorted(_RUN_KEYS - set(run))]
    if "run_id" in run and (not isinstance(run["run_id"], str) or not _RUN_ID.match(run["run_id"])):
        errors.append(f"run.run_id must be a run identifier without spaces or slashes: got {run['run_id']!r}")
    if "plugin" in run and (not isinstance(run["plugin"], str) or not _PLUGIN.match(run["plugin"])):
        errors.append(f"run.plugin must be a plugin name: got {run['plugin']!r}")
    version = run.get("plugin_version")
    if "plugin_version" in run and (not isinstance(version, str) or not _VERSION.match(version)):
        errors.append(f"run.plugin_version must be a release version such as 0.69.0: got {version!r}")
    for key, kind in (("kind", RunKind), ("harness", Harness), ("route", Route)):
        if key in run:
            errors += _enum_value(run[key], kind, f"run.{key}")
    return errors


def _class_errors(record: dict[str, Any]) -> list[str]:
    """Return errors for the class-specific rules: hardware fields, known limits and closing events.

    Examples:
        >>> _class_errors({"class": "hardware-absent", "hardware_class": "cuda", "reason": "no NVIDIA GPU",
        ...                "host_class": {"os": "linux", "architecture": "x86_64"}})
        []
        >>> _class_errors({"class": "environment-provisionable", "hardware_class": "cuda"})
        ['hardware_class and reason belong to the hardware-absent class only']
        >>> _class_errors({"class": "process", "known_limit": True})
        ['known_limit applies to the hardware-absent class only']
    """
    errors: list[str] = []
    hardware = record.get("class") == GapClass.HARDWARE_ABSENT.value
    if hardware:
        errors += _enum_value(record.get("hardware_class"), HardwareClass, "hardware_class")
        errors += _one_line_text(record.get("reason"), "reason")
        if _host_class(record.get("host_class")) is None:
            errors.append("hardware gaps require host_class with os and architecture")
    elif "hardware_class" in record or "reason" in record:
        errors.append("hardware_class and reason belong to the hardware-absent class only")
    if not hardware and "host_class" in record:
        errors.append("host_class belongs to the hardware-absent class only")
    known = record.get("known_limit", False)
    if not isinstance(known, bool):
        errors.append("known_limit must be true or false")
    elif known and not hardware:
        errors.append("known_limit applies to the hardware-absent class only")
    if (known is True or record.get("status") == Status.CLOSED.value) and record.get("deduction") not in (None, 0):
        errors.append("a known limit or closing event carries no deduction (null or 0)")
    return errors


def record_errors(record: object, *, historical: bool = False) -> list[str]:
    """Return every schema error of one ledger record; an empty list means the record is valid.

    Args:
        record: Parsed JSON value of one ledger line.
        historical: Preserve structural reading of events already accepted by earlier versions.

    Returns:
        Human-readable error messages, one per violated rule.

    Examples:
        >>> good = {"schema": "gap-ledger", "schema_version": 1, "id": "opencv-absent",
        ...         "class": "environment-provisionable", "summary": "cv2 not importable",
        ...         "owner": "workflow", "closable_by": "provision opencv-python in an ephemeral env",
        ...         "evidence": "checks/tests.stderr.txt",
        ...         "subject": {"repo": "octo/demo", "pr": 7, "head": "a1b2c3d"},
        ...         "run": {"run_id": "r1", "kind": "review", "harness": "codex", "plugin": "codex-rig",
        ...                 "plugin_version": "0.52.0", "route": "direct"}}
        >>> record_errors(good)
        []
        >>> record_errors({**good, "schema_version": 99})
        ['schema_version 99 is not supported (expected 1)']
    """
    if not isinstance(record, dict):
        return ["record must be a JSON object"]
    if record.get("schema") != SCHEMA:
        return [f"schema must be {SCHEMA!r}"]
    version = record.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        return [f"schema_version {version} is not supported (expected {SCHEMA_VERSION})"]
    errors = [f"unknown key {key!r}" for key in sorted(set(record) - _REQUIRED - _OPTIONAL)]
    errors += [f"{key} is required" for key in sorted(_REQUIRED - set(record))]
    errors += _slug(record.get("id"), "id")
    errors += _enum_value(record.get("class"), GapClass, "class")
    errors += _enum_value(record.get("owner"), Owner, "owner")
    if isinstance(record.get("id"), str):
        canonical = lookup_gap_id(record["id"])
        if canonical and (record.get("class"), record.get("owner")) != canonical:
            errors.append(f"{record['id']} requires class {canonical[0]} and owner {canonical[1]}")
        if canonical and canonical[0] == "hardware-absent" and record["id"] != f"{record.get('hardware_class')}-absent":
            errors.append("hardware_class must match the canonical gap id")
    errors += _enum_value(record.get("status", Status.OPEN.value), Status, "status")
    errors += _one_line_text(record.get("summary"), "summary")
    errors += _one_line_text(record.get("closable_by"), "closable_by")
    errors += _relative_path(record.get("evidence"), "evidence")
    errors += _deduction(record.get("deduction"))
    errors += _subject_errors(record.get("subject"))
    errors += _run_errors(record.get("run"))
    carried = record.get("carried_from")
    if carried is not None and (not isinstance(carried, str) or not _RUN_ID.match(carried)):
        errors.append(f"carried_from must be null or an earlier run_id: got {carried!r}")
    errors += _class_errors(record)
    if not historical and not errors:
        errors += _hardware_evidence_errors(record)
    return errors


def _hardware_evidence_errors(record: dict[str, Any]) -> list[str]:
    """Require current open hardware events to state an independent absence of the claimed class."""
    if record.get("class") != GapClass.HARDWARE_ABSENT.value or record.get("status") == Status.CLOSED.value:
        return []
    label = record["id"]
    hardware = record["hardware_class"]
    if absent_hardware_class(f"{label}: {record['reason']}") != hardware:
        return ["reason must state a device absence matching hardware_class, independent of software provisioning"]
    summary = record["summary"]
    if provisioning_cause(summary) and absent_hardware_class(f"{label}: {summary}") != hardware:
        return ["summary names a software provisioning cause for the gap"]
    return []


def _read_bytes(path: Path) -> bytes:
    """Read a whole file, refusing a symlink or an oversized file so a planted link cannot redirect the read.

    Raises:
        LedgerError: The path is a symlink, too large or unreadable.
    """
    if path.is_symlink():
        raise LedgerError(f"refusing symlinked file: {path}")
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise LedgerError(f"file larger than {MAX_FILE_BYTES} bytes: {path}")
        return path.read_bytes()
    except OSError as error:
        raise LedgerError(f"cannot read {path}: {error}") from error


def parse_lines(
    path: Path,
    data: bytes,
    first_line: int = 1,
    *,
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    historical: bool = False,
) -> Ledger:
    """Parse JSON-lines bytes into records, validating each and keeping line-numbered errors.

    Args:
        path: File the bytes came from, for messages.
        data: Raw file content.
        first_line: Line number of the first line, so staged records continue the ledger's numbering.
        transform: Optional emitter normalization of staged JSON objects, in memory.
        historical: Preserve structural reading of events already accepted by earlier versions.

    Returns:
        The parsed ledger; blank lines are skipped.

    Examples:
        >>> parse_lines(Path("gaps.jsonl"), b"not json\\n").errors
        ['gaps.jsonl:1: not valid JSON']
    """
    ledger = Ledger(path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        ledger.errors.append(f"{path}: not UTF-8")
        return ledger
    for offset, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        number = first_line + offset
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            ledger.errors.append(f"{path}:{number}: not valid JSON")
            continue
        if transform is not None and isinstance(record, dict):
            record = transform(record)
        ledger.errors += [f"{path}:{number}: {error}" for error in record_errors(record, historical=historical)]
        if isinstance(record, dict):
            ledger.records.append(record)
    return ledger


def consistency_errors(records: list[dict[str, Any]]) -> list[str]:
    """Return errors when records of one ledger disagree on run or subject, or carry a gap from their own run.

    Examples:
        >>> consistency_errors([])
        []
    """
    if not records:
        return []
    errors: list[str] = []
    run, subject = records[0].get("run"), records[0].get("subject")
    for index, record in enumerate(records[1:], start=2):
        if record.get("run") != run or record.get("subject") != subject:
            errors.append(f"record {index}: run or subject differs from record 1; one ledger holds one run")
    for index, record in enumerate(records, start=1):
        own = record.get("run", {}).get("run_id") if isinstance(record.get("run"), dict) else None
        if record.get("carried_from") is not None and record.get("carried_from") == own:
            errors.append(f"record {index}: carried_from names the record's own run")
    return errors


def _read_json(path: Path) -> object:
    """Read one JSON document, refusing a symlinked or oversized file.

    Raises:
        LedgerError: The file is a symlink, too large or unreadable.
        LedgerInvalidError: The content is not UTF-8 JSON.
    """
    try:
        return json.loads(_read_bytes(path).decode("utf-8"))
    except (ValueError, RecursionError) as error:
        raise LedgerInvalidError(f"{path}: not valid JSON") from error


def load_ledger(path: Path) -> Ledger:
    """Load and fully validate one ledger file; a missing file is an empty, valid ledger.

    Raises:
        LedgerError: The file is a symlink, too large or unreadable.
    """
    if not path.exists() and not path.is_symlink():
        return Ledger(path)
    ledger = parse_lines(path, _read_bytes(path), historical=True)
    ledger.errors += [f"{path}: {error}" for error in consistency_errors(ledger.records)]
    return ledger


def latest_by_id(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the last record per gap id, in first-seen order (readers take the last event per key).

    Examples:
        >>> list(latest_by_id([{"id": "a", "n": 1}, {"id": "b"}, {"id": "a", "n": 2}]).items())[0]
        ('a', {'id': 'a', 'n': 2})
    """
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        latest[record["id"]] = record
    return latest


def load_known_limits(path: Path) -> tuple[str, dict[str, KnownLimit]]:
    """Return the repository and the current host's known limits by ID.

    Only an absent hardware class with its exact reason qualifies; any other class is refused.

    Raises:
        LedgerError: The file is a symlink, too large or unreadable.
        LedgerInvalidError: The file is not JSON or violates the known-limits schema.

    Examples:
        >>> import tempfile, os
        >>> folder = tempfile.mkdtemp()
        >>> target = Path(folder) / "known-limits.json"
        >>> _ = target.write_text(json.dumps({"schema": "known-limits", "schema_version": 1, "repo": "octo/demo",
        ...     "limits": [{"id": "cuda-absent", "class": "hardware-absent", "hardware_class": "cuda",
        ...                 "reason": "This host has no NVIDIA GPU.",
        ...                 "host_class": host_class_fields(current_host_class())}]}), encoding="utf-8")
        >>> load_known_limits(target)[1]["cuda-absent"].hardware_class
        'cuda'
    """
    repo, entries = read_known_limits(path)
    host = current_host_class()
    return repo, {item.id: item for item in entries if item.host_class == host}


def read_known_limits(path: Path) -> tuple[str, list[KnownLimit]]:
    """Read the repository's validated limits across all host classes."""
    if path.parent.is_symlink():
        raise LedgerError(f"refusing symlinked known-limits directory: {path.parent}")
    data = _read_json(path)
    errors = _limits_file_errors(data)
    if errors:
        raise LedgerInvalidError("; ".join(f"{path}: {error}" for error in errors))
    limits = [
        KnownLimit(
            item["id"],
            item["hardware_class"],
            item["reason"],
            (item["host_class"]["os"], item["host_class"]["architecture"]),
        )
        for item in data["limits"]
    ]
    return data["repo"], limits


def _limits_file_errors(data: object) -> list[str]:
    """Return schema errors of a parsed known-limits file.

    Examples:
        >>> _limits_file_errors({"schema": "known-limits", "schema_version": 1, "repo": "octo/demo",
        ...     "limits": [{"id": "opencv-absent", "class": "environment-provisionable",
        ...                 "hardware_class": "cuda", "reason": "x"}]})[0]
        'limit 1: only an absent hardware class qualifies as a known limit; got class environment-provisionable'
    """
    if not isinstance(data, dict):
        return ["known-limits file must be a JSON object"]
    version = data.get("schema_version")
    if data.get("schema") != LIMITS_SCHEMA or type(version) is not int or version != LIMITS_SCHEMA_VERSION:
        return [f"schema must be {LIMITS_SCHEMA!r} version {LIMITS_SCHEMA_VERSION}"]
    errors = [f"unknown key {key!r}" for key in sorted(set(data) - _LIMITS_FILE_KEYS)]
    if not isinstance(data.get("repo"), str) or not _REPO.match(data["repo"]):
        errors.append(f"repo must be owner/name: got {data.get('repo')!r}")
    limits = data.get("limits")
    if not isinstance(limits, list):
        return [*errors, "limits must be a list"]
    seen: set[tuple[str, tuple[str, str] | None]] = set()
    for index, item in enumerate(limits, start=1):
        errors += [f"limit {index}: {error}" for error in _limit_errors(item)]
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            key = item["id"], _host_class(item.get("host_class"))
            if key in seen:
                errors.append(f"limit {index}: duplicate id {item['id']!r} for this host class")
            seen.add(key)
    return errors


def _limit_errors(item: object) -> list[str]:
    """Return schema errors of one known-limits entry."""
    if not isinstance(item, dict):
        return ["entry must be an object"]
    if item.get("class") != GapClass.HARDWARE_ABSENT.value:
        return [f"only an absent hardware class qualifies as a known limit; got class {item.get('class')}"]
    errors = [f"unknown key {key!r}" for key in sorted(set(item) - _LIMIT_KEYS)]
    errors += _slug(item.get("id"), "id")
    errors += _enum_value(item.get("hardware_class"), HardwareClass, "hardware_class")
    if _host_class(item.get("host_class")) is None:
        errors.append("host_class must contain os and architecture")
    if item.get("id") != f"{item.get('hardware_class')}-absent":
        errors.append("id must match hardware_class")
    reason = item.get("reason")
    reason_errors = _one_line_text(reason, "reason")
    if not reason_errors and not _known_limit_reason(reason, item.get("hardware_class")):
        reason_errors.append("reason must directly state a device absence matching hardware_class")
    return errors + reason_errors


def _known_limit_reason(reason: object, hardware_class: object) -> bool:
    """Require the whole reason to name exactly the absent hardware class."""
    if not isinstance(reason, str):
        return False
    clause = absent_device_clause(hardware_observation(reason, f"{hardware_class}-absent"))
    if clause is None or _device_classes(clause) != (hardware_class,):
        return False
    return clause["label"] is None or clause["label"].casefold() == hardware_class


def _known_limit_observation(record: dict[str, Any]) -> bool:
    """Use the exact reason as evidence and refuse a summary that qualifies the observation."""
    hardware = record.get("hardware_class")
    if not _known_limit_reason(record.get("reason"), hardware):
        return False
    if "summary" not in record:  # Consolidation checks a projected observation containing only the reason.
        return True
    summary = record["summary"]
    if not isinstance(summary, str):
        return False
    neutral = _NEUTRAL_LIMIT_SUMMARY.fullmatch(summary)
    if neutral is not None:
        return neutral["label"] is None or neutral["label"].casefold() == hardware
    return _known_limit_reason(summary, hardware)


def matching_limit(record: dict[str, Any], limits: Iterable[KnownLimit]) -> KnownLimit | None:
    """Find a limit whose ID, hardware class and observed host all match the record.

    The caller selects the limits of the record's repository. Both the observed and listed reasons must qualify so
    replacing one with the other preserves the match. The summary retains any additional provisioning evidence.
    """
    host = _host_class(record.get("host_class"))
    if host is None or not _known_limit_observation(record):
        return None
    return next(
        (
            item
            for item in limits
            if item.host_class == host
            and item.id == record.get("id")
            and item.hardware_class == record.get("hardware_class")
            and _known_limit_reason(item.reason, item.hardware_class)
        ),
        None,
    )


def apply_known_limit(record: dict[str, Any], limits: Iterable[KnownLimit]) -> dict[str, Any]:
    """Apply a matching known limit while preserving the observed gap's canonical ID."""
    listed = matching_limit(record, limits)
    if listed is None or record.get("status") == Status.CLOSED.value:
        return record
    return {**record, "reason": listed.reason, "known_limit": True, "deduction": None}


def register_known_limits(path: Path, repo: str, records: Iterable[dict[str, Any]]) -> tuple[list[str], str | None]:
    """Register open hardware observations for the current host, returning added IDs and any best-effort error.

    Only a schema-valid, open ``hardware-absent`` record of this repository observed on the current host qualifies,
    whose exact reason states an absent device and whose observation is free of provisioning causes. Writers are
    serialized by a lock directory beside the file. A lock older than ``STALE_LOCK_SECONDS`` belongs to a writer that
    died between taking and releasing it; it is removed once and the lock is taken again, so a crash never blocks
    registration for good. Two recoverers that both judge the same lock stale can still race on its removal; the
    registry itself is replaced atomically, so the worst outcome is one registration overwriting another's concurrent
    addition, which the next observation registers again.
    """
    host = current_host_class()
    fields = host_class_fields(host)
    candidates = {
        record["id"]: {key: record[key] for key in ("id", "class", "hardware_class", "reason", "host_class")}
        for record in records
        if record.get("class") == GapClass.HARDWARE_ABSENT.value
        and record.get("status") != Status.CLOSED.value
        and record.get("host_class") == fields
        and repo_key((record.get("subject") or {}).get("repo")) == repo_key(repo)
        and _known_limit_observation(record)
        and not record_errors(record)
    }
    if not candidates:
        return [], None
    lock = path.with_name(f".{path.name}.lock")
    try:
        if path.parent.is_symlink() or path.is_symlink():
            raise LedgerError(f"refusing symlinked known-limits destination: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize competing emitters across the read and replace. A busy lock only skips this registration.
        _take_lock(lock)
        try:
            return _register_limits_locked(path, repo, host, candidates), None
        finally:
            lock.rmdir()
    except (OSError, LedgerError, LedgerInvalidError) as error:
        return [], str(error)


def _take_lock(lock: Path) -> None:
    """Create the lock directory, first removing it once when its age shows the writer that held it died.

    Raises:
        FileExistsError: A live writer holds the lock.
        OSError: The lock cannot be created or a stale lock cannot be removed.
    """
    try:
        lock.mkdir()
        return
    except FileExistsError:
        if lock.is_symlink() or time.time() - lock.stat().st_mtime < STALE_LOCK_SECONDS:
            raise
    with contextlib.suppress(FileNotFoundError):
        lock.rmdir()
    lock.mkdir()


def _register_limits_locked(
    path: Path, repo: str, host: tuple[str, str], candidates: dict[str, dict[str, Any]]
) -> list[str]:
    """Add unlisted observations and atomically replace the validated registry."""
    entries: list[KnownLimit] = []
    if path.exists() or path.is_symlink():
        listed_repo, entries = read_known_limits(path)
        if repo_key(listed_repo) != repo_key(repo):
            raise LedgerInvalidError(f"known-limits repository {listed_repo} differs from {repo}")
        # Keep the file's own spelling of an existing registry's repository.
        repo = listed_repo
    for item in entries:
        if item.host_class == host:
            candidates.pop(item.id, None)
    if not candidates:
        return []
    limits = [
        {
            "id": item.id,
            "class": "hardware-absent",
            "hardware_class": item.hardware_class,
            "reason": item.reason,
            "host_class": host_class_fields(item.host_class),
        }
        for item in entries
    ]
    payload = {
        "schema": LIMITS_SCHEMA,
        "schema_version": LIMITS_SCHEMA_VERSION,
        "repo": repo,
        "limits": limits + list(candidates.values()),
    }
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temp = Path(handle.name)
            handle.write((json.dumps(payload, indent=2) + "\n").encode("utf-8"))
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return list(candidates)


def known_limit_errors(
    records: list[dict[str, Any]], repo: str, limits: dict[str, KnownLimit] | Iterable[KnownLimit]
) -> list[str]:
    """Return errors where a record disagrees with the repository's known limits.

    Verify claimed limits against the matching host observation. An unmarked first observation may retain its deduction.
    A ``known_limit`` claim on a record of another repository is refused: this file cannot verify it.

    Examples:
        >>> host = ("linux", "x86_64")
        >>> limits = {"cuda-absent": KnownLimit("cuda-absent", "cuda", "no NVIDIA GPU", host)}
        >>> errors = known_limit_errors([{"id": "cuda-absent", "subject": {"repo": "octo/demo"}, "deduction": 0.05,
        ...     "known_limit": True, "hardware_class": "cuda", "reason": "no NVIDIA GPU",
        ...     "host_class": host_class_fields(host)}], "octo/demo", limits)
        >>> errors[0].endswith("never deducted again")
        True
    """
    errors: list[str] = []
    entries = list(limits.values() if isinstance(limits, dict) else limits)
    for index, record in enumerate(records, start=1):
        if record.get("known_limit") is not True:
            continue
        subject = record.get("subject") if isinstance(record.get("subject"), dict) else {}
        gap_id = record.get("id")
        listed = matching_limit(record, entries) if repo_key(subject.get("repo")) == repo_key(repo) else None
        if listed is None:
            errors.append(f"record {index}: known_limit {gap_id} is not listed in the known-limits file")
            continue
        if record.get("deduction") not in (None, 0):
            errors.append(
                f"record {index}: listed known limit {gap_id} carries a deduction; "
                "a known limit is shown once, never deducted again"
            )
        if record.get("reason") != listed.reason:
            errors.append(f"record {index}: {gap_id} reason differs from the known-limits file")
    return errors


def staged_files(ledger: Path) -> list[Path]:
    """Return the staged ``.rec`` files of a ledger: the single writer's first, then parallel writers by name.

    Examples:
        >>> staged_files(Path("/nonexistent/gaps.jsonl"))
        []
    """
    single = ledger.with_name(f"{ledger.name}.rec")
    parallel = sorted(Path(match) for match in glob.glob(str(ledger.parent / f"{glob.escape(ledger.name)}.*.rec")))
    return [path for path in [single, *parallel] if path.exists() or path.is_symlink()]


def _with_limits(
    errors: list[str], records: list[dict[str, Any]], known: tuple[str, Iterable[KnownLimit]] | None
) -> list[str]:
    """Add known-limit cross-check errors; without a known-limits file every ``known_limit`` claim is refused.

    A zero-deduction known limit is honoured only when the repository's file lists it, so a claim this call cannot check
    is never accepted on the record's own word.
    """
    if known is None:
        return errors + [
            f"record {index}: known_limit {record.get('id')} cannot be verified without --known-limits"
            for index, record in enumerate(records, start=1)
            if record.get("known_limit") is True
        ]
    repo, limits = known
    return errors + known_limit_errors(records, repo, limits)


def _append_lines(ledger: Path, records: list[dict[str, Any]]) -> None:
    """Append records as one sorted-key JSON line each, first closing a final line left without a newline."""
    unterminated = False
    if ledger.exists():
        data = _read_bytes(ledger)
        unterminated = bool(data) and not data.endswith(b"\n")
    payload = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    with ledger.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(("\n" if unterminated else "") + payload)


def append_staged(
    ledger: Path,
    limits_file: Path | None = None,
    *,
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    known_limits: tuple[str, Iterable[KnownLimit]] | None = None,
) -> tuple[int, list[str]]:
    """Validate the staged records against the whole extended ledger, then append them and delete the staged files.

    Args:
        ledger: The run's ``gaps.jsonl``.
        limits_file: Optional known-limits file to cross-check against.
        transform: Optional emitter normalization applied before validation, without rewriting the stage.
        known_limits: Optional validated registry snapshot, preferred to rereading the registry file.

    Returns:
        ``(appended, errors)``; on any error nothing is appended and the staged files stay for correction.

    Raises:
        LedgerError: A file is a symlink, too large or unreadable.
    """
    if ledger.is_symlink():
        raise LedgerError(f"refusing symlinked ledger: {ledger}")
    current = load_ledger(ledger)
    staged = staged_files(ledger)
    if not staged:
        return 0, [f"nothing staged: write records to {ledger.name}.rec first"]
    errors = list(current.errors)
    new_records: list[dict[str, Any]] = []
    next_line = len(current.records) + 1
    for path in staged:
        parsed = parse_lines(path, _read_bytes(path), first_line=next_line, transform=transform)
        errors += parsed.errors
        new_records += parsed.records
        next_line += len(parsed.records)
    extended = current.records + new_records
    if known_limits is None and limits_file is not None:
        known_limits = read_known_limits(limits_file)
    errors = _with_limits(errors + consistency_errors(extended), new_records, known_limits)
    if errors:
        return 0, errors
    _append_lines(ledger, new_records)
    for path in staged:
        path.unlink()
    return len(new_records), []


def identity_errors(identity: object) -> list[str]:
    """Return errors of a carry identity file: an object with exactly ``subject`` and ``run``."""
    if not isinstance(identity, dict) or set(identity) != {"subject", "run"}:
        return ["identity must be an object with exactly the keys subject and run"]
    return _subject_errors(identity["subject"]) + _run_errors(identity["run"])


def carry_ineligible(prior: dict[str, Any], identity: dict[str, Any]) -> str | None:
    """Name why the prior run's gaps cannot be carried into the new run, or return ``None`` when they can.

    Gaps carry only between runs on the same repository, pull request and head, of the same kind, plugin and plugin
    version: any other change can have closed or opened a gap, so the new run records its gaps fresh.

    Examples:
        >>> base = {"subject": {"repo": "o/r", "pr": 1, "head": "abcdef1"},
        ...         "run": {"run_id": "r1", "kind": "review", "plugin": "oss", "plugin_version": "0.69.0"}}
        >>> carry_ineligible(base, {**base, "run": {**base["run"], "run_id": "r2"}}) is None
        True
        >>> carry_ineligible(base, {"subject": {**base["subject"], "head": "1234567"},
        ...                         "run": {**base["run"], "run_id": "r2"}})
        'head differs'
    """
    if repo_key(prior["subject"].get("repo")) != repo_key(identity["subject"].get("repo")):
        return "repository differs"
    for key, label in (("pr", "pull request"), ("head", "head")):
        if prior["subject"].get(key) != identity["subject"].get(key):
            return f"{label} differs"
    for key, label in (("kind", "run kind"), ("plugin", "plugin"), ("plugin_version", "plugin version")):
        if prior["run"].get(key) != identity["run"].get(key):
            return f"{label} differs"
    if prior["run"].get("run_id") == identity["run"].get("run_id"):
        return "same run"
    return None


def _carried_limits(records: list[dict[str, Any]], limits: tuple[KnownLimit, ...] | None) -> list[dict[str, Any]]:
    """Re-derive the known-limit flag of carried records from the current registry, never from the prior run.

    ``limits`` is the new run's repository registry, or ``None`` when no registry of that repository applies. A
    record the registry lists becomes a known limit without a deduction. A record the prior run flagged that the
    registry no longer lists (no file, a file of another repository, or a deleted entry) is not carried: a known
    limit stores no deduction, so its share cannot be restored here, and the new run records the gap fresh with its
    own deduction.

    Examples:
        >>> flagged = {"id": "cuda-absent", "known_limit": True, "deduction": None}
        >>> _carried_limits([flagged, {"id": "lint-not-run", "deduction": 0.02}], None)
        [{'id': 'lint-not-run', 'deduction': 0.02}]
    """
    carried = []
    for record in records:
        claimed = record.get("known_limit") is True
        base = {key: value for key, value in record.items() if key != "known_limit"}
        current = apply_known_limit(base, limits) if limits is not None else base
        if claimed and current.get("known_limit") is not True:
            continue
        carried.append(current)
    return carried


def carry_forward(
    prior_path: Path,
    identity: dict[str, Any],
    ledger: Path,
    limits_file: Path | None = None,
    *,
    known_limits: tuple[str, Iterable[KnownLimit]] | None = None,
) -> int:
    """Append every open gap of an eligible prior run to the new run's ledger as ``carried_from`` events.

    A known limit is carried only while the given registry of the new run's repository still lists it; see
    ``_carried_limits``.

    Returns:
        Number of records appended.

    Raises:
        LedgerError: A file is a symlink, too large or unreadable.
        LedgerInvalidError: Invalid identity or prior ledger, or a refused extended ledger.
        CarryIneligibleError: The prior run is not on the same head, pull request or plugin version.
    """
    if ledger.is_symlink():
        raise LedgerError(f"refusing symlinked ledger: {ledger}")
    errors = identity_errors(identity)
    prior = load_ledger(prior_path)
    errors += prior.errors
    if errors:
        raise LedgerInvalidError("; ".join(errors))
    if not prior.records:
        return 0
    reason = carry_ineligible(prior.records[0], identity)
    if reason is not None:
        raise CarryIneligibleError(reason)
    carried = [
        {**record, "run": identity["run"], "subject": identity["subject"], "carried_from": record["run"]["run_id"]}
        for record in latest_by_id(prior.records).values()
        if record.get("status", Status.OPEN.value) == Status.OPEN.value
        and (record["class"] != "hardware-absent" or _host_class(record.get("host_class")) == current_host_class())
    ]
    if known_limits is None and limits_file is not None:
        known_limits = read_known_limits(limits_file)
    limits: tuple[KnownLimit, ...] | None = None
    if known_limits is not None:
        repo, listed = known_limits
        limits = tuple(listed) if repo_key(repo) == repo_key(identity["subject"]["repo"]) else None
    carried = _carried_limits(carried, limits)
    current = load_ledger(ledger)
    extended = current.records + carried
    snapshot = (identity["subject"]["repo"], limits) if limits is not None else None
    problems = _with_limits(current.errors + consistency_errors(extended), carried, snapshot)
    problems += [error for record in carried for error in record_errors(record)]
    if problems:
        raise LedgerInvalidError("; ".join(problems))
    if carried:
        _append_lines(ledger, carried)
    return len(carried)


def schema_description() -> dict[str, Any]:
    """Return the closed value sets and key lists of the current schema, for emitters and documentation.

    Examples:
        >>> schema_description()["schema_version"]
        1
    """
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "required": sorted(_REQUIRED),
        "optional": sorted(_OPTIONAL),
        "subject": sorted(_SUBJECT_KEYS),
        "run": sorted(_RUN_KEYS),
        "class": [member.value for member in GapClass],
        "owner": [member.value for member in Owner],
        "run.kind": [member.value for member in RunKind],
        "run.harness": [member.value for member in Harness],
        "run.route": [member.value for member in Route],
        "status": [member.value for member in Status],
        "hardware_class": [member.value for member in HardwareClass],
        "known_limits": {
            "schema": LIMITS_SCHEMA,
            "schema_version": LIMITS_SCHEMA_VERSION,
            "entry": sorted(_LIMIT_KEYS),
            "host_class": ["os", "architecture"],
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate, append and carry forward a run's gap ledger.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "append"):
        sub = commands.add_parser(name)
        sub.add_argument("ledger", type=Path)
        sub.add_argument("--known-limits", type=Path, default=None)
    carry = commands.add_parser("carry")
    carry.add_argument("ledger", type=Path)
    carry.add_argument("--from", dest="prior", type=Path, required=True)
    carry.add_argument("--identity", type=Path, required=True)
    carry.add_argument("--known-limits", type=Path, default=None)
    limits = commands.add_parser("limits")
    limits.add_argument("file", type=Path)
    commands.add_parser("schema")
    lookup = commands.add_parser("lookup", help="show the canonical class and owner of a gap id")
    lookup.add_argument("id")
    return parser


def _report(errors: list[str], ok_line: str) -> int:
    """Print errors (exit 1) or the success line (exit 0)."""
    if errors:
        print("\n".join(errors))
        return EXIT_INVALID
    print(ok_line)
    return EXIT_OK


def _run(args: argparse.Namespace) -> int:
    """Dispatch one subcommand; ``LedgerError`` propagates to ``main``."""
    if args.command == "schema":
        print(json.dumps(schema_description(), indent=2))
        return EXIT_OK
    if args.command == "lookup":
        classification = lookup_gap_id(args.id)
        if classification is None:
            return _report([f"unlisted gap id: {args.id}"], "")
        print(json.dumps({"id": args.id, "class": classification[0], "owner": classification[1]}))
        return EXIT_OK
    if args.command == "limits":
        repo, limits = load_known_limits(args.file)
        return _report([], f"OK: {len(limits)} known limit(s) for {repo}")
    if args.command == "validate":
        ledger = load_ledger(args.ledger)
        known = read_known_limits(args.known_limits) if args.known_limits is not None else None
        errors = _with_limits(ledger.errors, ledger.records, known)
        return _report(errors, f"OK: {len(ledger.records)} record(s) in {args.ledger}")
    if args.command == "append":
        appended, errors = append_staged(args.ledger, args.known_limits)
        return _report(errors, f"appended {appended} record(s) to {args.ledger}")
    identity = _read_json(args.identity)
    if not isinstance(identity, dict):
        raise LedgerInvalidError(f"{args.identity}: identity must be a JSON object")
    appended = carry_forward(args.prior, identity, args.ledger, args.known_limits)
    return _report([], f"carried {appended} record(s) into {args.ledger}")


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; see the module docstring for subcommands and exit codes.

    Argument errors and ``--help`` exit through argparse's own ``SystemExit`` (code 2 and 0).
    """
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except CarryIneligibleError as error:
        print(f"carry: not-eligible: {error}; record this run's gaps fresh")
        return EXIT_NOT_ELIGIBLE
    except LedgerInvalidError as error:
        print(error)
        return EXIT_INVALID
    except (LedgerError, OSError) as error:
        print(f"I/O failure: {error}")
        return EXIT_IO


if __name__ == "__main__":
    sys.exit(main())
