#!/usr/bin/env python
"""Reproduce a pull request's performance and memory claims: base against head, interleaved, on this host.

## Purpose

    Author benchmark tables are not evidence until they reproduce. This helper extracts the claims from the PR text,
    decides per claim whether this host's hardware class can test it, and runs base and head through one benchmark in
    alternating processes, comparing the relative effect (never the author's absolute numbers).

## Trust model

    PR text is data: claims are parsed with regular expressions and stored, never executed. A benchmark is
    contributed code on both sides (it imports the base tree and the head tree), so ``run`` executes it only as the
    review's contributor-code gate decided, read from ``$RUN_DIR/code-clause.txt``:

    - the same-repo clause, and only when the frozen ``pr-meta.json`` also says ``isCrossRepository: false`` -- runs
      directly in isolated checkouts of the planned commits;
    - the approval clause -- every run goes through ``untrusted_code_guard.py run --approval-file`` with the approval
      files the parent bound for each side (the harness and benchmark are bound files), and the guard refuses any
      command, tier or tree the approval does not cover;
    - anything else -- nothing runs; every claim that needed a run is ``not-run``.

    This helper never binds an approval and has no option that skips the gate.

## Usage

    python review_claim_repro.py extract --pr-meta FILE [--comments-json FILE] [--text-file FILE ...] [--out FILE]
    python review_claim_repro.py plan --claims FILE --env FILE --repo DIR --base REF --head REF --work-dir DIR
        [--bench FILE] [--claim ID ...] [--reps N] [--warmup N] [--out FILE]
    python review_claim_repro.py run --plan FILE --clause-file FILE [--pr-meta FILE] [--head-approval FILE]
        [--base-approval FILE] [--rounds N] [--timeout SEC] [--budget SEC] [--out FILE]

    ``plan`` clones both commits into ``--work-dir`` with ``git clone --shared``. It writes the harness beside
    the benchmark and prints the exact command and bound files for each side.

## Benchmark protocol

    The benchmark file defines ``bench()`` and optionally ``setup()`` (its return value is passed to ``bench``). The
    harness times ``bench`` with ``time.perf_counter`` (warm-up calls, then the median of ``--reps`` calls) and prints
    one JSON line. When a memory claim exists it also measures the resident-memory peak the first call adds above the
    post-``setup()`` peak (``rss_growth_mb``; ``None`` where the host has no peak-RSS reading, such as native Windows)
    and the ``tracemalloc`` peak of one extra call. Each round runs base and head in separate processes in ABBA order,
    so drift on the host affects both sides equally. A memory claim is judged only on the memory it names
    (``memory_kind``): ``heap`` on the tracemalloc peak, ``rss`` (also unqualified memory) on RSS growth, and
    ``peak-rss`` on total process peak RSS. Device memory and allocation counts have no sample source and are
    ``not-run``.

## Outputs

    One claim record per claim: ``claim_text``, ``metric`` (with ``memory_kind`` on extracted claims), ``direction``
    (always ``lower-is-better``: time and memory are measured, throughput as time per call), ``author_hw``,
    ``host_spec``, ``comparable_hardware``
    (``decision`` same-class, proportional or not-comparable, plus ``basis`` and ``reason``), ``method`` (``command``,
    ``repeats``, ``warmup``, ``order: alternating``), ``base_samples_n``, ``head_samples_n``, ``base_median``,
    ``head_median``, ``relative_delta`` (``(head - base) / base``, signed), ``spread`` (the larger interquartile range
    of the two sides divided by the base median), ``claimed_relative``, ``verdict``, ``reason``. The shape is shared
    with the Codex Rig benchmark claim record. Verdicts:

    - ``reproduced`` -- at least ``MIN_ROUNDS`` samples per side, the head moves the metric in the claimed direction
      by more than ``max(spread, MIN_EFFECT)``, and by at least half the claimed relative effect when the claim states
      one;
    - ``not-reproduced`` -- the run completed and the effect is within noise, opposite, or under half the claim;
    - ``hardware-unavailable`` -- the claim needs an OS or accelerator class this run's host lacks; the reason reads
      ``not reproducible on this host: requires <CLASS>; ...``, names that class and its probe evidence and nothing
      broader. The host class (``host_class``: OS, architecture, accelerators) is recorded per run as a fact; on a
      host of the claim's class nothing about the host is reported;
    - ``no-benchmark`` -- no benchmark file was supplied, or the supplied one measures other claims (``--claim``);
    - ``not-run`` -- the gate gave no approval, refused a run, the environment was unavailable or the benchmark
      failed; never reported as ``not-reproduced``.

## Used by

    oss:review ``modes/claim-repro.md``.

Exit codes:
    0 -- a JSON document was printed (every outcome above, failures included)
    2 -- bad arguments or unreadable input files
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_env_provision as provision

SCHEMA = 1
#: Relative change below this is never called an effect, however tight the spread.
MIN_EFFECT = 0.02
#: A measured effect must reach this fraction of the claimed relative effect to count as reproduced.
CLAIM_FRACTION = 0.5
DEFAULT_ROUNDS = 5
DEFAULT_REPS = 20
DEFAULT_WARMUP = 3
DEFAULT_TIMEOUT = 900
#: Seconds for all rounds together; the review block runs under a 600 s tool cap.
DEFAULT_BUDGET = 540
#: Samples per side (one per round) below which no verdict is drawn: a budget stop with fewer is ``not-run``.
MIN_ROUNDS = 3
#: Characters of one claim line kept in a record.
CLAIM_TEXT_CHARS = 300
#: Lines of a failed run's output kept as evidence.
TAIL_LINES = 12
#: Key that marks the harness result line among whatever the benchmark itself prints.
RESULT_MARKER = "claim_repro"
#: The review's contributor-code gate, a sibling script; this helper only ever calls its ``run`` mode.
GUARD = Path(__file__).resolve().with_name("untrusted_code_guard.py")
#: The clause the review writes for a same-repo PR (run directly, no guard).
SAME_REPO_CLAUSE = "PR code: run tests directly in the checkout; no guard."
_APPROVAL_CLAUSE = re.compile(
    r'^PR code: run every command that loads or executes PR code only as python -I "[^"]+" run --approval-file "[^"]+"'
)
_GIT = shutil.which("git") or "git"


class Verdict(str, Enum):
    """Outcome of one claim."""

    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not-reproduced"
    HARDWARE_UNAVAILABLE = "hardware-unavailable"
    NO_BENCHMARK = "no-benchmark"
    NOT_RUN = "not-run"


class ClaimReproError(Exception):
    """Bad input that ends the command with exit status 2."""


# --------------------------------------------------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------------------------------------------------

#: One stated number: digit groups ("1,200") are one number, never two.
_NUM_TEXT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_NUM = rf"({_NUM_TEXT})"
#: Unit -> (dimension, factor to the dimension's base unit: seconds or bytes).
_UNITS: dict[str, tuple[str, float]] = {
    "ns": ("time", 1e-9), "µs": ("time", 1e-6), "us": ("time", 1e-6), "ms": ("time", 1e-3), "s": ("time", 1.0),
    "sec": ("time", 1.0), "secs": ("time", 1.0), "second": ("time", 1.0), "seconds": ("time", 1.0),
    "millisecond": ("time", 1e-3), "milliseconds": ("time", 1e-3), "microsecond": ("time", 1e-6),
    "microseconds": ("time", 1e-6), "nanosecond": ("time", 1e-9), "nanoseconds": ("time", 1e-9),
    "b": ("memory", 1.0), "byte": ("memory", 1.0), "bytes": ("memory", 1.0),
    "kb": ("memory", 1e3), "kib": ("memory", 1024.0), "mb": ("memory", 1e6),
    "mib": ("memory", 1024.0**2), "gb": ("memory", 1e9), "gib": ("memory", 1024.0**3),
}  # fmt: skip
_UNIT = "|".join(sorted(_UNITS, key=len, reverse=True))
_TIME_UNIT = "|".join(unit for unit, (dimension, _) in _UNITS.items() if dimension == "time")
_MEMORY_UNIT = "|".join(unit for unit, (dimension, _) in _UNITS.items() if dimension == "memory")
_PER_SECOND = r"(?:\s*/\s*s|\s+per\s+second)"
_RATE_UNIT = (
    rf"fps|(?:frames?|it(?:eration)?s?|img|images?|samples?|ops?|requests?|tokens?|items?|{_MEMORY_UNIT}){_PER_SECOND}"
)
_PAIR_UNIT = rf"(?:{_RATE_UNIT})\b|(?:{_UNIT})\b(?:\s*/\s*[a-z][\w-]*)?"
_ARROW = r"->|→|=>"
_MEMORY_WORDS = re.compile(
    rf"\b(?:memory|ram|vram|rss|resident set size|allocations?|footprint|peak mem)\b|\d\s*(?:{_MEMORY_UNIT})\b", re.I
)
_RATE_WORDS = re.compile(rf"\b(?:throughput|[qr]ps|(?:read|write|transfer) speed|{_RATE_UNIT})\b", re.I)
_RATE_QUANTITY = re.compile(_RATE_UNIT, re.I)
_RATE_ALIASES = {"fps": "frame", "img": "image", "it": "iteration"}
_DIMENSION_METRIC = {"time": "time", "memory": "memory", "rate": "throughput"}
_DEVICE_MEMORY = re.compile(r"\b(?:vram|gpu (?:memory|mem|ram)|device memory|cuda memory|video memory)\b", re.I)
_HEAP_MEMORY = re.compile(r"\b(?:allocations?|allocated|heap|tracemalloc|python (?:memory|objects?))\b", re.I)
_ALLOCATION = re.compile(r"\ballocations?\b", re.I)
_ALLOCATION_BYTES = re.compile(
    rf"\ballocations?\s*(?:\(\s*|(?:[a-z]+\s+)?from\s+(?:{_NUM_TEXT})\s*)(?:{_MEMORY_UNIT})\b", re.I
)
_RSS_GROWTH = re.compile(r"\b(?:rss|resident (?:set|memory))\s+(?:growth|increase|delta)\b", re.I)
_PEAK_MEMORY = re.compile(
    r"\b(?:peak\s+(?:resident (?:set(?: size)?|memory)|rss|memory|ram|mem)|(?:rss|memory|ram)\s+(?:peak|high.water mark))\b",
    re.I,
)
_UNPARSED_REASON = "claim not parsed"
_ARCH_ALIASES = {"x86_64": ("x86_64", "x86-64", "amd64", "x64", "win64"), "arm64": ("arm64", "aarch64")}
_ARCH_FAMILIES = {alias: family for family, aliases in _ARCH_ALIASES.items() for alias in aliases}
_HW_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tensorrt", re.compile(r"\btensor\s?rt\b|\btrt\b", re.IGNORECASE)),
    (
        "cuda",
        re.compile(
            r"\bcuda\b|\bnvidia\b|\b(?:rtx|gtx)\s?\d{3,4}\w*|\b(?:t4|a10g?|a100|h100|h200|v100|l4|l40s?|p100|"
            r"k80|jetson\w*)\b",
            re.IGNORECASE,
        ),
    ),
    ("rocm", re.compile(r"\brocm\b|\bmi\d{2,3}x?\b|\bradeon\b", re.IGNORECASE)),
    # Only the device API in its Apple sense: "M2 Pro" or "Apple silicon" names the machine (a CPU family), and a
    # "bare-metal" server is no GPU at all; "Metal" binds only beside Apple or a GPU-API noun.
    (
        "mps",
        re.compile(
            r"\bmps\b|\bmetal\s+performance\s+shaders\b|\bapple\s+metal\b"
            r"|\bmetal\s+(?:backend|api|gpu|device|kernels?|shaders?|compute)\b",
            re.IGNORECASE,
        ),
    ),
    ("tpu", re.compile(r"\btpu\b", re.IGNORECASE)),
    ("gpu", re.compile(r"\bgpus?\b", re.IGNORECASE)),
    (
        "cpu-x86_64",
        re.compile(
            rf"\b(?:{'|'.join(_ARCH_ALIASES['x86_64'])}|x86|intel|xeon|ryzen|epyc|core\s+i[3579])\b", re.IGNORECASE
        ),
    ),
    (
        "cpu-arm64",
        re.compile(
            rf"\b(?:{'|'.join(_ARCH_ALIASES['arm64'])}|graviton\d?|apple\s+m\d|m\d\s+(?:pro|max|ultra)|apple silicon)\b",
            re.IGNORECASE,
        ),
    ),
    ("cpu", re.compile(r"\bcpu\b", re.IGNORECASE)),
)
#: Accelerator classes a claim may require, mapped to the host classes that satisfy them.
ACCELERATOR_NEEDS = {"tensorrt": ("cuda",), "cuda": ("cuda",), "rocm": ("rocm",), "mps": ("mps",), "tpu": ("tpu",),
                     "gpu": ("cuda", "rocm")}  # fmt: skip
#: How a reason names the required class: ``requires <label>; host has no <CLASS> device``.
ACCELERATOR_LABELS = {"tensorrt": "CUDA (TensorRT)", "cuda": "CUDA", "rocm": "ROCm", "mps": "MPS", "tpu": "TPU",
                      "gpu": "CUDA or ROCm (an unnamed GPU)"}  # fmt: skip
ACCELERATOR_CLASS_NAMES = {"cuda": "CUDA", "rocm": "ROCm", "mps": "MPS", "tpu": "TPU"}
#: Operating systems a claim line can name, keyed by the ``platform.system()`` value the host record carries.
_OS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("windows", re.compile(r"\b(?:windows|win(?:32|64))\b", re.I)),
    ("linux", re.compile(r"\blinux\b|\bubuntu\b|\bdebian\b|\bfedora\b|\bcentos\b|\bwsl2?\b", re.IGNORECASE)),
    ("macos", re.compile(r"\bmac ?os(?: ?x)?\b|\bos ?x\b|\bdarwin\b", re.IGNORECASE)),
)
_NON_OS_WINDOWS = re.compile(
    r"\b(?:(?:sliding|rolling|time|attention|tumbling|hopping)\s+windows|windows\s+of)\b", re.I
)
_OS_WORD = "|".join(pattern.pattern for _, pattern in _OS_PATTERNS)

_HEAD_SUBJECT = r"(?:this branch|(?:(?:this|the|our)\s+)?(?:pr|change|patch|head)|this|it)"
_HEAD_BASELINE = re.compile(_HEAD_SUBJECT, re.I)
_HEAD_PREFIX = re.compile(rf"{_HEAD_SUBJECT}\s+", re.I)
_OPERATION = (
    r"(?:inference|training|load(?:er|ing)?|decod(?:e|er|ing)|encod(?:e|er|ing)|pars(?:e|er|ing)|"
    r"tokeniz(?:e|er|ing|ation)|compil(?:e|er|ing|ation)|render(?:er|ing)?|(?:im|ex)port(?:ing)?|"
    r"NMS(?: over (?:sliding|rolling) windows)?|lookups?|work(?:load)?|path join|loop)"
)
_PERF_NOUN = (
    rf"(?:(?:peak|mean|median|average|startup|load|loading|wall|gpu|device|host|resident|per-batch|NMS|Python|{_OS_WORD})"
    rf"\s+){{0,3}}(?:latency|throughput|runtimes?|(?:run|execution|response|load|loading|startup|wall) times?|time|timing|duration|"
    rf"(?:read |write |transfer )?speed|performance|memory|ram|vram|rss|resident set size|[qr]ps|"
    rf"allocation counts?|(?:number of\s+)?allocations?|footprint|{_RATE_UNIT})\b"
    r"(?:\s+(?:use|usage|consumption))?"
)
_MEASUREMENT = rf"(?:{_PERF_NOUN}|{_OPERATION}\b)"
_ARTICLE = r"(?:the\s+)?"
_SAVED_VALUE = rf"(?:{_NUM_TEXT})\s*(?:{_PAIR_UNIT})\b"
_VALUE_TEXT = rf"(?:{_NUM_TEXT})(?:\s*(?:{_PAIR_UNIT})\b)?"
_VALUE = re.compile(rf"(?P<number>{_NUM_TEXT})(?:\s*(?P<unit>{_PAIR_UNIT})\b)?", re.I)
_NEGATOR = re.compile(r"\b(?:no|not|never|without)\b|n['’]t\b", re.I)
_INLINE_CODE = re.compile(r"(?P<ticks>\x60+).*?(?P=ticks)(?!\x60)")

# These are the lexical classes for the grammatical slots and the arithmetic sense of each one.
_LESS_VERB = (r"reduce[sd]?|reducing|decrease[sd]?|decreasing|cut(?:s|ting)?|drop(?:s|ped|ping)?|"
              r"lower(?:s|ed|ing)?|shr(?:[iau]nk|inking)s?|save[sd]?|saving|shave[sd]?|shaving|"
              r"halv(?:e[sd]?|ing)|fall(?:s|ing)?|fell")  # fmt: skip
_MORE_VERB = (
    r"increase[sd]?|increasing|grow(?:s|ing|n)?|grew|ris(?:e[sn]?|ing)|rose|rais(?:e[sd]?|ing)|doubl(?:e[sd]?|ing)"
)
_FASTER_VERB = r"speed(?:s|ing)?[- ]up|sped up|accelerat(?:e[sd]?|ing)"
_SLOWER_VERB = r"slow(?:s|ed|ing)?(?:[- ]down)?"
_IMPROVE_VERB = r"improv(?:e[sd]?|ing)"
_EFFECT_VERB = rf"(?:{_LESS_VERB}|{_MORE_VERB}|{_FASTER_VERB}|{_SLOWER_VERB}|{_IMPROVE_VERB})"
_TRANSITION_VERB = rf"(?:{_EFFECT_VERB}|go(?:es)?|went|chang(?:e[sd]?|ing))"
_QUANTITY_DEGREE = r"less|fewer|lower|smaller|more|higher|larger"
_SPEED_DEGREE = r"faster|slower|quicker"
_DEGREE_PREDICATE = rf"{_QUANTITY_DEGREE}|{_SPEED_DEGREE}|better"
_EFFECT_SENSES = {
    "faster": re.compile(rf"faster|quicker|{_FASTER_VERB}", re.I),
    "slower": re.compile(rf"slower|{_SLOWER_VERB}", re.I),
    "improvement": re.compile(rf"better|{_IMPROVE_VERB}", re.I),
    "less": re.compile(rf"less|fewer|smaller|reduction|{_LESS_VERB}", re.I),
    "more": re.compile(rf"more|higher|larger|{_MORE_VERB}", re.I),
}
_APPROX = r"(?:~\s*|(?:about|around|roughly|up to|over|nearly|almost)\s+)?"
_PERCENT = rf"{_APPROX}(?:{_NUM_TEXT})\s*%"
_FACTOR = rf"(?:{_NUM_TEXT})\s*[x×]"
_DEGREE_MAG = rf"(?:(?P<percent>{_PERCENT})\s+|(?P<factor>{_FACTOR})\s*)?"
_BY_MAG = rf"(?:\s+by\s+(?:(?P<percent>{_PERCENT})|(?P<factor>{_FACTOR})|{_SAVED_VALUE}))?"
_EFFECT_WORD = r"\w+(?:-\w+)?"
_BY_MEASUREMENT = rf"\s+by\s+(?:{_PERCENT}|{_FACTOR}|{_SAVED_VALUE})"
_PAIR_VALUES = rf"(?P<before>{_VALUE_TEXT})\s*(?:{_ARROW}|\bto\b)\s*(?P<after>{_VALUE_TEXT})"
_ARROW_VALUES = rf"(?P<before>{_VALUE_TEXT})\s*(?:{_ARROW})\s*(?P<after>{_VALUE_TEXT})"
_DURATION_PAIR = rf"(?P<after>{_SAVED_VALUE})\s+instead of\s+(?P<before>{_SAVED_VALUE})"
_TRANSITION_SUBJECT = rf"(?:(?:{_HEAD_SUBJECT})\s+)?(?:now\s+)?"
_CHANGED_OBJECT = rf"(?P<object>{_PERF_NOUN}|it)"
_PAIR_END = r"(?:\s+(?:now|with this (?:pr|change|patch|branch)))?"
_DEGREE_PRED = r"(?:is|are|runs?|became|becomes?)"
_MEASURE_VERB = r"(?:uses?|takes?|gives?|achieves?|delivers?|gets?|consumes?|requires?)"
_ELAPSED_TIME = re.compile(r"\btakes?\s*$", re.I)
_NON_BARE_TIME = r"(?!time\b)"
_ENV = rf"(?:{_OS_WORD}|(?:{'|'.join(_ARCH_FAMILIES)})|CUDA|MPS|ROCm|(?:my\s+)?M\d\s+Pro(?:\s+laptop)?)"
_SIZE = r"(?:\d+[kKmM]?\s+(?:boxes|samples|frames|images|tokens|requests|rows)|(?:the\s+)?(?:large|small)\s+(?:batches|inputs|requests|configs))"
_SCOPE = (
    rf"(?:\s+(?:(?:on|for)\s+{_SIZE}|on\s+{_ENV}|during\s+{_OPERATION}|of\s+(?:the cache|big files)|"
    r"per\s+(?:worker|epoch|call|batch|request)|significantly|here|in exchange for safety|"
    r"when the (?:batch|input) size (?:increases|decreases) from \d+ to \d+))*"
)
_PARENTHETICAL_DELTA = rf"(?:\s+\({_SAVED_VALUE}\s+(?:{_QUANTITY_DEGREE})\))?"
_BASELINE = (
    rf"(?:{_HEAD_SUBJECT}|before|what we previously shipped|(?:the\s+)?(?:previous release|old implementation|baseline|"
    r"base|main|develop|pure Python|v?\d+(?:\.\d+)+))\b"
)
_COMPARE_INTRO = r"(?:compared\s+(?:to|with|against)|relative\s+to|vs\.?)"
_COMPARATOR_TEXT = (
    rf"\s+(?:than|{_COMPARE_INTRO})\s+(?P<baseline>{_BASELINE})"
    rf"(?:,\s+where\s+{_PERF_NOUN}\s+{_TRANSITION_VERB}\s+from\s+{_VALUE_TEXT}\s+to\s+{_VALUE_TEXT})?"
)
_COMPARATOR = rf"(?:{_COMPARATOR_TEXT})?"
_LEADING_CONTEXT = re.compile(
    rf"^(?:{_COMPARE_INTRO}\s+(?P<baseline>{_BASELINE}),\s*|on\s+{_ENV}(?:\s+\d+)?(?:,\s*|\s+)"
    rf"|{_OS_WORD}(?:\s*:\s*|\s+))",
    re.I,
)
_METRIC_HEADING = rf"(?:(?:{_EFFECT_VERB}|{_QUANTITY_DEGREE})\s+)?{_PERF_NOUN}"
_SUPPORTING_PAIR_TEXT = rf",\s+from\s+{_SAVED_VALUE}\s+to\s+{_SAVED_VALUE}"
_SUPPORTING_PAIR = rf"(?:{_SUPPORTING_PAIR_TEXT})?"
_ATTACHED_COMPARISON = re.compile(rf"{_SUPPORTING_PAIR_TEXT}|{_COMPARATOR_TEXT}", re.I)
_BEFORE_AFTER_DELIMITER = r"(?:[,;]\s*)?\bafter\s*:?\s*"

# Direction is granted only by a full match in this table. Use a measured subject/object or a head
# subject (possibly elided). Each row names its grammatical shape, and only its captured slots feed the calculation.
_HEAD_SHAPES = tuple(
    (shape, re.compile(pattern, re.I))
    for shape, pattern in (
        ("verb and measured object", rf"{_TRANSITION_SUBJECT}(?P<effect>{_EFFECT_VERB})\s+{_ARTICLE}"
         rf"(?P<object>{_PERF_NOUN}|{_OPERATION}|it|{_SAVED_VALUE}(?:\s+of\s+{_PERF_NOUN})?){_BY_MAG}{_SCOPE}"
         rf"{_COMPARATOR}(?::\s*{_ARROW_VALUES})?"),
        ("measured subject and verb", rf"{_ARTICLE}(?P<subject>{_PERF_NOUN})\s+(?P<verb>{_EFFECT_VERB}){_BY_MAG}"),
        ("measured subject and degree", rf"{_ARTICLE}(?P<subject>{_MEASUREMENT})\s+"
         rf"(?:(?:now\s+)?{_DEGREE_PRED}\s+(?:now\s+)?){_DEGREE_MAG}(?P<effect>{_DEGREE_PREDICATE})"
         rf"(?:\s+now)?(?:\s+\((?:{_RATE_UNIT})\))?{_COMPARATOR}{_SCOPE}{_SUPPORTING_PAIR}"),
        ("head makes measured object comparative", rf"{_TRANSITION_SUBJECT}mak(?:e|es|ing)\s+{_ARTICLE}"
         rf"(?P<object>{_MEASUREMENT})\s+{_DEGREE_MAG}(?P<effect>{_DEGREE_PREDICATE}){_COMPARATOR}{_SCOPE}"),
        ("comparative measurement", rf"{_TRANSITION_SUBJECT}(?:(?:{_MEASURE_VERB})\s+)?{_DEGREE_MAG}"
         rf"(?P<effect>{_QUANTITY_DEGREE}|{_SPEED_DEGREE})\s+{_ARTICLE}{_NON_BARE_TIME}"
         rf"(?P<object>{_PERF_NOUN}){_SCOPE}{_COMPARATOR}{_PARENTHETICAL_DELTA}"),
        ("elapsed time comparative", rf"{_TRANSITION_SUBJECT}takes?\s+{_DEGREE_MAG}"
         rf"(?P<effect>{_QUANTITY_DEGREE})\s+{_ARTICLE}(?P<object>time){_SCOPE}{_COMPARATOR}"),
        ("nominal effect", rf"(?P<percent>{_PERCENT})\s+(?P<effect>{_EFFECT_WORD})\s+in\s+"
         rf"{_ARTICLE}(?P<object>{_PERF_NOUN}){_SCOPE}{_COMPARATOR}"),
        ("speed summary", rf"{_TRANSITION_SUBJECT}(?:(?:{_DEGREE_PRED})\s+(?:now\s+)?)?{_DEGREE_MAG}"
         rf"(?P<effect>{_SPEED_DEGREE}){_COMPARATOR}{_SCOPE}"),
        ("measured transition", rf"(?:{_TRANSITION_SUBJECT}(?P<effect>{_TRANSITION_VERB})(?:\s+{_ARTICLE}{_CHANGED_OBJECT})?"
         rf"|{_ARTICLE}(?P<subject>{_PERF_NOUN})\s+(?:now\s+)?(?P<verb>{_TRANSITION_VERB}))"
         rf"{_BY_MAG}\s+(?:from\s+)?{_PAIR_VALUES}{_PAIR_END}"),
        ("arrow measurement", rf"(?:(?P<subject>{_METRIC_HEADING})(?:\s*:\s*|\s+)|takes?\s+)?{_ARROW_VALUES}"),
        ("before/after measurement", rf"(?:(?P<subject>{_PERF_NOUN})\s*:\s*|(?:(?P<object>{_PERF_NOUN})\s+))?"
         rf"before\s*:?\s*(?P<before>{_VALUE_TEXT})\s*{_BEFORE_AFTER_DELIMITER}(?P<after>{_VALUE_TEXT})"),
        ("combined before/after heading", rf"(?:(?P<subject>{_PERF_NOUN})\s*:?\s+)?"
         rf"before\s*/\s*after\s*:?\s*{_PAIR_VALUES}"),
        ("before and now measurement", rf"(?P<subject>{_PERF_NOUN})\s+was\s+(?P<before>{_VALUE_TEXT})"
         rf"\s+before(?: this (?:pr|change|patch))?\s+and\s+is\s+(?P<after>{_VALUE_TEXT})\s+now"),
        ("duration instead of baseline", rf"(?:(?P<subject>{_OPERATION})\s+now\s+|{_TRANSITION_SUBJECT})takes?\s+"
         rf"{_DURATION_PAIR}"),
        ("unclassified verb and measured magnitude", rf"{_TRANSITION_SUBJECT}{_EFFECT_WORD}\s+"
         rf"{_ARTICLE}(?P<object>{_PERF_NOUN}){_BY_MEASUREMENT}{_SCOPE}{_COMPARATOR}"),
    )
)  # fmt: skip

_CANDIDATE_VALUE = rf"(?:{_NUM_TEXT})[kKmM]?(?:\s*(?:{_PAIR_UNIT})\b)?"
_BETWEEN_VALUES = (
    rf"(?:{_ARROW}|\b(?:to|from|(?P<instead>instead of)|(?:rather )?than|vs\.?)\s+|"
    rf"\(?\s*was\s+|(?P<after_label>{_BEFORE_AFTER_DELIMITER})|before(?: this (?:pr|change|patch))?\s+and\s+is\s+|"
    rf"[,;]\s*(?:{_HEAD_SUBJECT}\s+)?(?:now\s+(?:it\s+takes?\s+)?|brings?\s+it\s+to\s+))"
)
_CANDIDATE_PAIR = re.compile(
    rf"(?<![\w.,+−-])(?P<before>{_CANDIDATE_VALUE})\s*{_BETWEEN_VALUES}\s*(?P<after>{_CANDIDATE_VALUE})"
    r"(?!\w|[.,]\d|\s*[%/])",
    re.I,
)
_PAIR_LABEL = re.compile(
    rf"\b(?P<label>{_PERF_NOUN})(?:\s+{_TRANSITION_VERB})?\s*:?\s*"
    r"(?:(?:from|before(?:\s*/\s*after)?)\s*:?\s*)?$",
    re.I,
)
_MEASURED_VALUE_PREFIX = re.compile(rf"\b{_PERF_NOUN}\s+(?:now\s+)?is\s+(?:now\s+)?$", re.I)
_BEFORE_LABEL = re.compile(r"\bbefore\s*:?\s*$", re.I)
_DURATION_PURPOSE = re.compile(r"^\s+to\s+[^\W\d_]+\b", re.I)
_DURATION_VALUE = re.compile(rf"(?:{_NUM_TEXT})\s*(?:{_TIME_UNIT})", re.I)
_MEASURED_OBJECT_END = (
    r"(?=$|\s*[,;:!?()]|\.(?![\w(])|\s+(?:by|from|of|in|on|for|during|per|with|when|than|and|required|significantly)\b)"
)
_NUMBERED_OUTCOME = re.compile(
    rf"\b(?P<label>{_PERF_NOUN})(?=\s*(?::\s*)?(?:{_NUM_TEXT})|\s+(?:on|is|are|was|were|at|stands?)\b)", re.I
)
_CANDIDATE_OBJECT = rf"{_ARTICLE}(?P<object>{_PERF_NOUN}){_MEASURED_OBJECT_END}"
_SPEED_OBJECT = rf"{_ARTICLE}(?P<object>{_OPERATION})\b"
_WRITTEN_FRACTION = r"half|(?:one )?(?:third|quarter)"
_CANDIDATE_DEGREE = rf"(?:(?:{_PERCENT}|{_FACTOR}|twice as|(?:{_WRITTEN_FRACTION})(?: of)?)\s*)?"
_CANDIDATE_SHAPES = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        rf"\b{_EFFECT_WORD}\s+{_CANDIDATE_OBJECT}{_BY_MEASUREMENT}",
        rf"\b{_EFFECT_VERB}\s+(?:{_CANDIDATE_OBJECT}|{_SAVED_VALUE}{_MEASURED_OBJECT_END}|{_PERCENT}\s+off\s+{_ARTICLE}"
        rf"(?P<quantity>{_PERF_NOUN}){_MEASURED_OBJECT_END})",
        rf"\b(?P<object>{_PERF_NOUN})\s+(?:(?:is|are|was|were|now|have|has|been|previously|never)\s+)*"
        rf"(?:{_EFFECT_VERB}|{_CANDIDATE_DEGREE}(?:{_DEGREE_PREDICATE}|worse))\b",
        rf"\b(?:{_FASTER_VERB}|{_SLOWER_VERB}|{_SPEED_DEGREE})\s+{_SPEED_OBJECT}{_MEASURED_OBJECT_END}",
        rf"\bmak(?:e|es|ing)\s+{_SPEED_OBJECT}\s+{_CANDIDATE_DEGREE}(?:{_SPEED_DEGREE})\b",
        rf"\b(?:{_CANDIDATE_DEGREE}(?:{_QUANTITY_DEGREE}|{_SPEED_DEGREE})\b|(?:{_WRITTEN_FRACTION})(?: of)?)"
        rf"\s+{_ARTICLE}{_NON_BARE_TIME}(?P<object>{_PERF_NOUN}){_MEASURED_OBJECT_END}",
        rf"\btakes?\s+(?:{_CANDIDATE_DEGREE}(?:{_QUANTITY_DEGREE})|(?:{_WRITTEN_FRACTION})(?: of)?)"
        rf"\s+{_ARTICLE}(?P<object>time)\b",
        rf"\b(?:{_PERCENT}|{_FACTOR})\s+(?P<object>{_PERF_NOUN})(?:\s+(?:improvement|reduction|increase))?\b"
        rf"{_MEASURED_OBJECT_END}",
        rf"\b(?P<object>{_PERF_NOUN})\s+(?:improvement|reduction|increase)\s+(?:of\s+)?(?:{_PERCENT}|{_FACTOR})",
        rf"\b(?:{_PERCENT}|{_FACTOR})\s+{_EFFECT_WORD}\s+in\s+{_CANDIDATE_OBJECT}",
        rf"\b(?:{_PERCENT}|{_FACTOR}|twice as)\s*(?:{_SPEED_DEGREE}|fast\b|slowdown|speed[- ]?up)\b",
        rf"\b(?:{_DEGREE_PRED}|be|mak(?:e|es)\s+anything)\s+(?:(?:now|not|no longer)\s+)?(?:{_SPEED_DEGREE})\b",
        rf"\b(?P<object>{_OPERATION})\s+runs?\s+without\s+slowing down\b",
        r"\b(?:fix(?:es|ed|ing)?|resolv(?:es?|ed|ing))\s+(?:a|the)\s+(?:slowdown|memory leak)\b",
    )
)
_BOUND_PRONOUN = re.compile(rf"\b{_EFFECT_VERB}\s+it\s+by\s+{_PERCENT}", re.I)
_CLAUSE_BOUNDARY = re.compile(r"(?:\s*,\s*|\s+)(?:and|but|whereas|while)\s+|,(?!\d)|;", re.I)
_PARALLEL_DELTA = re.compile(
    rf"(?:{_OS_WORD})\s+sees?\s+(?:a|an)\s+{_PERCENT}\s+(?:increase|decrease|reduction)\b", re.I
)
_VERB_SUMMARY = re.compile(
    rf"{_TRANSITION_SUBJECT}[a-z]+s\s+(?:a|an|the)\s+[\w-]+(?:\s+[\w-]+)?"
    r"(?:\s+from\s+\d+\s+to\s+\d+\s+\w+)?",
    re.I,
)
#: Report labels of the OS classes the provisioner records (``review_env_provision.OS_CLASSES``).
OS_LABELS = {"windows": "Windows", "linux": "Linux", "macos": "macOS"}
#: Every not-comparable reason starts with this, so a report reads it as a statement about this run's host only.
HOST_PREFIX = "not reproducible on this host: "
_FENCE = re.compile(r"```(?:python|py)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _hardware(text: str) -> list[str]:
    """Return the hardware classes a text names, most specific first."""
    return [kind for kind, pattern in _HW_PATTERNS if pattern.search(text)]


def _hardware_phrases(text: str) -> list[str]:
    """Return the literal hardware phrases a text names (for ``author_hw``)."""
    phrases: list[str] = []
    for _, pattern in _HW_PATTERNS:
        for match in pattern.finditer(text):
            phrase = match.group(0).strip()
            if phrase.lower() not in (p.lower() for p in phrases):
                phrases.append(phrase)
    return phrases


def _quantity_metric(label: str, family: str | None = None) -> str | None:
    """Resolve a measured quantity from its normalized unit, then its named outcome."""
    if family and (metric := _DIMENSION_METRIC.get(family.partition(":")[0])):
        return metric
    if _RATE_WORDS.search(label):
        return "throughput"
    if _MEMORY_WORDS.search(label) or _HEAP_MEMORY.search(label) or _DEVICE_MEMORY.search(label):
        return "memory"
    return "time" if re.search(rf"\b{_PERF_NOUN}", label, re.I) else None


def _to_float(text: str) -> float:
    """Read one stated number: digit-group commas are dropped and a Unicode minus is a minus.

    Example:
        >>> _to_float("1,200.5"), _to_float("−3")
        (1200.5, -3.0)
    """
    return float(text.replace(",", "").replace("−", "-"))


def _scaled(number: str, unit: str | None) -> tuple[float, str | None]:
    """Return a normalized value and its comparable unit family.

    Time units scale to seconds and memory units to bytes. A rate retains its numerator per second. ``%`` is a
    ``percent`` no measured quantity has; no unit has no dimension (``None``), so the caller falls back to a header.

    Examples:
        >>> _scaled("500", "ms"), _scaled("1.5", "GB"), _scaled("2.5", "x"), _scaled("12", None)
        ((0.5, 'time'), (1500000000.0, 'memory'), (2.5, 'rate'), (12.0, None))
    """
    key = (unit or "").lower().strip()
    numerator, *denominator = re.split(r"\s*/\s*|\s+per\s+", key, maxsplit=1)
    if _RATE_QUANTITY.fullmatch(key):
        if numerator in _UNITS:
            return _to_float(number) * _UNITS[numerator][1], "rate:byte"
        numerator = numerator.removesuffix("s") if numerator != "fps" else numerator
        return _to_float(number), f"rate:{_RATE_ALIASES.get(numerator, numerator)}"
    if numerator in _UNITS:
        dimension, factor = _UNITS[numerator]
        family = f"{dimension}:{denominator[0].removesuffix('s')}" if denominator else dimension
        return _to_float(number) * factor, family
    return _to_float(number), _CELL_DIMENSIONS.get(key)


def _ratio(before: float, after: float, metric: str) -> tuple[float | None, str] | None:
    """Turn a before/after pair of one quantity into ``(relative change of the measured quantity, direction)``.

    Time and memory are measured directly; a rate is converted to time per call. A transition involving zero has
    direction only. Equal or negative quantities state no directional claim.

    Examples:
        >>> _ratio(120, 80, "time"), _ratio(100, 125, "throughput"), _ratio(30, 0, "throughput")
        ((-0.3333, 'decrease'), (-0.2, 'decrease'), (None, 'increase'))
    """
    if not all(map(math.isfinite, (before, after))) or before < 0 or after < 0 or before == after:
        return None
    start, end = (after, before) if metric == "throughput" else (before, after)
    direction = "decrease" if end < start else "increase"
    relative = end / start - 1 if start and end else None
    return (round(relative, 4) if relative is not None and math.isfinite(relative) else None), direction


def _pair_claim(
    before: tuple[float, str | None],
    after: tuple[float, str | None],
    label_metric: str | None,
    *,
    comparison: bool = True,
) -> tuple[str, float | None, str | None] | None:
    """Resolve identity and comparison role before deriving a relative effect from a pair.

    Examples:
        >>> _pair_claim((2.0, "time"), (0.5, "time"), "throughput")
        ('time', -0.75, 'decrease')
        >>> _pair_claim((2.0, "time:request"), (0.5, "time:batch"), "time")
        ('time', None, None)
        >>> _pair_claim((2.0, None), (0.5, None), None) is None
        True
    """
    base, base_family = before
    head, head_family = after
    first = _quantity_metric("", base_family)
    second = _quantity_metric("", head_family)
    metric = first if first and first == second else label_metric
    if metric is None or before == after:
        return None
    if not comparison or base_family != head_family or (base_family and not first):
        return metric, None, None
    if effect := _ratio(base, head, metric):
        return metric, *effect
    return metric, None, None


def _pair_values(before: str, after: str) -> tuple[tuple[float, str | None], tuple[float, str | None]] | None:
    """Normalize both values, inheriting a trailing shared unit for a unitless first value."""
    first, second = _VALUE.fullmatch(before), _VALUE.fullmatch(after)
    if not first or not second:
        return None
    start = _scaled(first["number"], first["unit"] or second["unit"])
    end = _scaled(second["number"], second["unit"])
    return start, end


def _matched_effect(match: re.Match[str], antecedent: str | None) -> tuple[str, float | None, str | None] | None:
    """Read only the measurement, effect and magnitude slots captured by a complete head shape."""
    slots = match.groupdict()
    target = slots.get("object") or slots.get("subject")
    effect = slots.get("effect") or slots.get("verb")
    sense = _verb_word(effect) if effect else None
    metric = antecedent if target and target.lower() == "it" else (_quantity_metric(target or "") or "time")
    percent = slots.get("percent")
    if metric is None or (percent and not sense):
        return None
    direction = _effect_direction(sense, metric) if sense else None
    if percent:
        value = _to_float(re.search(_NUM, percent)[1]) / 100
        return metric, *_percent_relative(value, effect, metric)
    if factor := slots.get("factor"):
        value = _to_float(factor[:-1].strip())
        if value <= 1:
            return metric, None, None
        relative = 1 / value - 1 if direction == "decrease" else value - 1
        return metric, round(relative, 4), direction
    if slots.get("before"):
        return _matched_pair(slots["before"], slots["after"], _quantity_metric(target or ""), direction)
    return (metric, None, direction) if direction else None


def _matched_pair(
    before: str, after: str, metric: str | None, direction: str | None
) -> tuple[str, float | None, str | None] | None:
    """Use the numeric pair, or keep only the explicitly stated direction if its values contradict it."""
    values = _pair_values(before, after)
    if values:
        first, second = values
        measured = _quantity_metric("", first[1])
        if first[1] != second[1] and (measured == _quantity_metric("", second[1]) or direction is None):
            return None
        if first == second:
            return (measured or metric or "time"), None, None
        # A named effect still supplies direction if an attached pair measures something else.
        if not metric or not measured or measured == metric or (direction is None and _VALUE.fullmatch(before)["unit"]):
            pair = _pair_claim(first, second, metric)
            if pair and (not direction or direction == pair[2]):
                return pair
    return (metric, None, direction) if metric and direction else None


def _complete_claim(
    line: str, antecedent: str | None, implicit: bool = True
) -> tuple[tuple[str, float | None, str | None] | None, bool]:
    """Read one full assertion and whether it gives a head subject to a following predicate.

    Examples:
        >>> _complete_claim("2x faster", None), _complete_claim("uses 40% less memory", None)
        ((('time', -0.5, 'decrease'), True), (('memory', -0.4, 'decrease'), True))
    """
    context = _LEADING_CONTEXT.match(line)
    if context:
        if _HEAD_BASELINE.fullmatch(context["baseline"] or ""):
            return None, False
        line = line[context.end() :]
    head = implicit or bool(_HEAD_PREFIX.match(line))
    if not head:
        return None, False
    # A metric heading can qualify another complete claim, such as a more specific measured arrow.
    heading, separator, remainder = line.partition(":")
    candidates = [line]
    if separator and re.fullmatch(_METRIC_HEADING, heading, re.I):
        candidates.append(remainder.strip())
    for candidate in candidates:
        for _, pattern in _HEAD_SHAPES:
            match = pattern.fullmatch(candidate)
            if match:
                slots = match.groupdict()
                if _HEAD_BASELINE.fullmatch(slots.get("baseline") or ""):
                    return None, False
                return _matched_effect(match, antecedent), not slots.get("subject")
    return None, bool(_VERB_SUMMARY.fullmatch(line))


def _line_claim(
    line: str, antecedent: str | None = None, *, implicit: bool = True
) -> tuple[tuple[str, float | None, str | None] | None, bool]:
    """Read a full head assertion or a coordinated predicate with a positively established head subject.

    Examples:
        >>> _line_claim("Improves throughput: latency 1,200 ms -> 800 ms")[0]
        ('time', -0.3333, 'decrease')
    """
    if _NEGATOR.search(line):
        return None, False
    line = line.lstrip(" \t#*>-").rstrip(".!? ")
    return _complete_claim(line, antecedent, implicit)


def _verb_word(verb: str) -> str:
    """Normalize a comparison to its measurement sense, independent of the presence of a number.

    Example:
        >>> _verb_word("Cuts"), _verb_word("increases"), _verb_word("speeds up")
        ('less', 'more', 'faster')
    """
    return next((sense for sense, pattern in _EFFECT_SENSES.items() if pattern.fullmatch(verb)), "")


def _effect_direction(sense: str, metric: str) -> str:
    """Translate a comparison's sense to the direction of the measured quantity (time per call for throughput)."""
    if sense in {"faster", "improvement"}:
        return "decrease"
    if sense == "slower":
        return "increase"
    return "decrease" if (sense == "less") != (metric == "throughput") else "increase"


def _percent_relative(value: float, word: str, metric: str) -> tuple[float | None, str]:
    """Convert ``<value>% <word>`` into the relative change of the measured quantity and its direction.

    "30% faster" means 1.3x the speed, so time per call changes by ``1/1.3 - 1``; "30% less" changes the metric by
    ``-0.3``; higher throughput lowers time per call and lower throughput raises it (``1/0.8 - 1`` for 20% lower). A
    fall of the whole quantity (100% or more) has no magnitude a measurement could reach.

    Examples:
        >>> _percent_relative(0.3, "faster", "time")
        (-0.2308, 'decrease')
        >>> _percent_relative(0.25, "higher", "throughput"), _percent_relative(0.2, "lower", "throughput")
        ((-0.2, 'decrease'), (0.25, 'increase'))
        >>> _percent_relative(1.0, "less", "memory")
        (None, 'decrease')
    """
    sense = _verb_word(word)
    direction = _effect_direction(sense, metric)
    if sense == "faster" or (sense == "improvement" and metric != "memory"):
        relative = 1 / (1 + value) - 1
    elif metric == "throughput" and sense in {"more", "less"}:
        rate = 1 + value if sense == "more" else 1 - value
        relative = 1 / rate - 1 if rate > 0 else None
    elif direction == "decrease":
        relative = -value if value < 1 else None
    else:
        relative = value
    return round(relative, 4) if relative is not None else None, direction


def _is_table_row(line: str) -> bool:
    """Tell whether a line is a Markdown table row (``| a | b |``)."""
    return line.strip().startswith("|") and line.strip().endswith("|") and line.count("|") >= 3


def _lines(text: str) -> list[str]:
    """Split PR prose into claim-sized sentences; code fences and table rows are left to their own rules."""
    stripped = _FENCE.sub(" ", text)
    out = []
    for raw in stripped.splitlines():
        if _is_table_row(raw):
            continue
        for part in re.split(r"(?<=[.!?])\s+(?=[A-Z])", raw.strip()):
            if part.strip():
                out.append(part.strip())
    return out


def _cells(row: str) -> list[str]:
    """Split one table row into stripped cells."""
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """Return ``(header cells, data rows)`` for every Markdown table outside code fences."""
    tables: list[tuple[list[str], list[list[str]]]] = []
    rows: list[str] = []
    for raw in [*_FENCE.sub(" ", text).splitlines(), ""]:
        if _is_table_row(raw):
            rows.append(raw)
            continue
        if len(rows) >= 3 and _TABLE_SEPARATOR.match(rows[1]):
            tables.append((_cells(rows[0]), [_cells(r) for r in rows[2:]]))
        rows = []
    return tables


_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}")
_BEFORE_HEAD = re.compile(r"^(?:before|base(?:line)?|develop|main|master|old|current|original|upstream)\b", re.I)
_AFTER_HEAD = re.compile(r"^(?:after|this pr|pr|new|head|patched|ours|optimi[sz]ed|branch|fix(?:ed)?)\b", re.I)
_TRANSITION_HEAD = re.compile(r"\b(?:before\s*(?:[/→]|->|\bto\b)\s*after|transition)\b", re.I)
_CELL_UNIT = rf"(?:{_PAIR_UNIT}|x|×|%)"
_CELL_DIMENSIONS = {"x": "rate", "×": "rate", "%": "percent"}
_CELL_TRANSITION = re.compile(rf"^{_NUM}\s*({_CELL_UNIT})?\s*(?:->|→|=>|\bto\b)\s*{_NUM}\s*({_CELL_UNIT})?$", re.I)
_HEADER_UNIT = re.compile(rf"\(\s*({_CELL_UNIT})\s*\)", re.IGNORECASE)


def _header_unit(header: str) -> str | None:
    """Return the unit a column header names in parentheses, or ``None``."""
    match = _HEADER_UNIT.search(header)
    return match.group(1) if match else None


#: A measurement cell: one number, an optional unit and an optional parenthetical ("339.8", "12 ms", "−17.4 (−5.1%)").
_NUMERIC_CELL = re.compile(
    rf"^[~≈]?\s*([-−]?(?:{_NUM_TEXT}))\s*({_CELL_UNIT})?\s*(?:\([^)]*\))?$",
    re.IGNORECASE,
)


def _cell_quantity(cell: str, header_unit: str | None) -> tuple[float, str | None] | None:
    """Return a measurement cell's ``(scaled value, dimension)``; the cell's own unit wins over its header's.

    ``None`` for prose such as "fails after 3 s".

    Examples:
        >>> _cell_quantity("500 ms", "s"), _cell_quantity("1,200", "ms"), _cell_quantity("fails after 3 s", None)
        ((0.5, 'time'), (1.2, 'time'), None)
    """
    match = _NUMERIC_CELL.match(cell.strip("*` "))
    return _scaled(match.group(1), match.group(2) or header_unit) if match else None


def _transition_claims(
    row: list[str], header: list[str], label: str
) -> list[tuple[str, str, float | None, str | None]]:
    """Collect cell comparisons and retain unlabeled measured intervals without direction."""
    found: list[tuple[str, str, float | None, str | None]] = []
    for col, cell in enumerate(row):
        match = _CELL_TRANSITION.match(cell)
        if not match:
            continue
        heading = header[col]
        comparison = bool(_TRANSITION_HEAD.search(heading))
        metric = _quantity_metric(label) or _quantity_metric(heading)
        descriptor = _TRANSITION_HEAD.sub("", _HEADER_UNIT.sub("", heading)).strip(" :-")
        if comparison and descriptor and not metric:
            continue
        unit = _header_unit(label) or _header_unit(heading)
        before = _scaled(match[1], match[2] or match[4] or unit)
        after = _scaled(match[3], match[4] or match[2] or unit)
        if got := _pair_claim(before, after, metric, comparison=comparison):
            found.append((f"{label} · {heading}: {cell}", *got))
    return found


def _column_role(heading: str) -> tuple[str, str]:
    """Read a revision role and the outcome name left on the column after removing it."""
    descriptor = _HEADER_UNIT.sub("", heading).strip().lower()
    if _TRANSITION_HEAD.search(descriptor):
        return "", ""
    for role, prefix in (("before", _BEFORE_HEAD), ("after", _AFTER_HEAD)):
        if match := prefix.match(descriptor):
            return role, descriptor[match.end() :].strip(" :-")
        if descriptor.endswith(f" {role}"):
            return role, descriptor[: -len(role)].strip(" :-")
    return "", ""


def _column_pairs(row: list[str], header: list[str], roles: list[tuple[str, str]], label: str) -> Iterator[tuple]:
    """Associate numeric revision cells by their outcome names, consuming each after column once."""
    numbers = [_cell_quantity(cell, _header_unit(heading) or _header_unit(label)) for cell, heading in zip(row, header)]
    available = [i for i, (role, _) in enumerate(roles) if role == "after" and numbers[i] is not None]
    for before, (role, name) in enumerate(roles):
        if role != "before" or numbers[before] is None or (name and not _quantity_metric(name)):
            continue
        matches = [
            i
            for i in available
            if (not roles[i][1] or _quantity_metric(roles[i][1])) and (not name or roles[i][1] in {"", name})
        ]
        if matches:
            after = min(matches, key=lambda i: (roles[i][1] != name, i < before, abs(i - before)))
            available.remove(after)
            metric = _quantity_metric(label) or _quantity_metric(name or roles[after][1])
            if got := _pair_claim(numbers[before], numbers[after], metric):
                yield (f"{label}: {header[before]} {row[before]} -> {header[after]} {row[after]}", *got)


def table_claims(text: str) -> list[tuple[str, str, float | None, str | None]]:
    """Extract ``(claim text, metric, relative, direction)`` from Markdown tables.

    Compare numeric revision columns by named outcome or read an explicit transition column. An interval in a
    measured column stays unparsed if its header gives the endpoints no revision roles.

    Examples:
        >>> rows = "| Hardware | Before (µs/call) | After (µs/call) |\\n|---|---|---|\\n| Ryzen 7 | 339.8 | 322.5 |\\n"
        >>> [(m, r, d) for _, m, r, d in table_claims(rows)]
        [('time', -0.0509, 'decrease')]
        >>> cells = "| masks | Latency Before/After (ms) | Peak RSS Before/After (MiB) |\\n"
        >>> cells += "|---|---|---|\\n| 50 | 39 to 9 | 117 to 7 |\\n"
        >>> [(m, r) for _, m, r, _ in table_claims(cells)]
        [('time', -0.7692), ('memory', -0.9402)]
        >>> units = "| step | before | after |\\n|---|---|---|\\n| load | 2 s | 500 ms |\\n"
        >>> [(m, r) for _, m, r, _ in table_claims(units)]
        [('time', -0.75)]
    """
    found: list[tuple[str, str, float | None, str | None]] = []
    for header, rows in _tables(text):
        roles = [_column_role(heading) for heading in header]
        for row in (cells for cells in rows if len(cells) == len(header)):
            label = " · ".join(
                cell for cell in row if not (_CELL_TRANSITION.fullmatch(cell) or _NUMERIC_CELL.fullmatch(cell))
            )[:120]
            found.extend(_column_pairs(row, header, roles, label))
            found.extend(_transition_claims(row, header, label))
    return found


def _memory_kind(text: str) -> str:
    """Distinguish device memory, allocation counts, Python heap bytes, and process RSS.

    Unqualified memory ("40% less memory", a GB figure) is the process's resident memory.

    Example:
        >>> _memory_kind("VRAM drops 40%"), _memory_kind("fewer allocations"), _memory_kind("uses less memory")
        ('device', 'allocation-count', 'rss')
    """
    if _DEVICE_MEMORY.search(text):
        return "device"
    if _ALLOCATION.search(text) and not _ALLOCATION_BYTES.search(text):
        return "allocation-count"
    if _HEAP_MEMORY.search(text):
        return "heap"
    return "peak-rss" if _PEAK_MEMORY.search(text) and not _RSS_GROWTH.search(text) else "rss"


def _operating_systems(text: str) -> list[str]:
    """Read OS names after masking non-platform uses of the plural 'windows'."""
    environment = _NON_OS_WINDOWS.sub(" ", text)
    return [name for name, pattern in _OS_PATTERNS if pattern.search(environment)]


def _claim_record(
    label: str, text: str, metric: str, rel: float | None, direction: str | None, context: str
) -> dict[str, object]:
    """Build one claim record; hardware named in ``text`` binds the claim, hardware the ``context`` names describes it.

    Only an OS, CPU architecture or accelerator named on the claim line itself is the claim's requirement (``requires``,
    ``requires_os``, ``requires_arch``); one named elsewhere in the body is the author's environment (``accelerator``
    with ``author_hw.scope`` ``document``, ``author_os``), compared with a caveat. A device-memory claim requires a GPU
    class even when the line names none.
    """
    memory_kind = _memory_kind(text) if metric == "memory" else None
    line_hw = _hardware(text)
    if memory_kind == "device" and not any(k in ACCELERATOR_NEEDS for k in line_hw):
        line_hw = ["gpu", *line_hw]
    doc_hw = _hardware(context)
    line_os = _operating_systems(text)
    doc_os = _operating_systems(context)
    hw = [*line_hw, *doc_hw]
    record = {
        "source": label, "claim_text": text[:CLAIM_TEXT_CHARS], "metric": metric, "memory_kind": memory_kind,
        "claimed_relative": rel, "expected_direction": direction,
        "author_hw": {"phrases": _hardware_phrases(text) or _hardware_phrases(context),
                      "scope": "line" if line_hw or line_os else ("document" if doc_hw or doc_os else "unstated")},
        "accelerator": next((k for k in hw if k in ACCELERATOR_NEEDS), None),
        "cpu_family": next((k.split("-", 1)[1] for k in hw if k.startswith("cpu-")), None),
        "requires": [k for k in line_hw if k in ACCELERATOR_NEEDS][:1],
        "requires_os": line_os,
        "requires_arch": [k.split("-", 1)[1] for k in line_hw if k.startswith("cpu-")],
        "author_os": next(iter(doc_os), None),
    }  # fmt: skip
    if direction is None:
        record.update(parse_status="unparsed", verdict=Verdict.NOT_RUN.value, reason=_UNPARSED_REASON)
    return record


def _pair_candidate(clause: str, pair: re.Match[str]) -> str | None:
    """Identify the stated measurement behind a pair, including incompatible rates."""
    prefix = clause[: pair.start()]
    label = _PAIR_LABEL.search(prefix)
    if pair["after_label"] and not _BEFORE_LABEL.search(prefix):
        return None
    if pair["instead"] and not (label or _ELAPSED_TIME.search(prefix) or _MEASURED_VALUE_PREFIX.search(prefix)):
        return None
    before, after = (re.sub(r"(?<=\d)[kKmM](?=\s|$)", "", pair[key]) for key in ("before", "after"))
    values = _pair_values(before, after)
    if values:
        first, second = values
        if label:
            return _quantity_metric(label["label"])
        metric = _quantity_metric("", first[1])
        if metric == _quantity_metric("", second[1]):
            return metric
    return None


def _claim_candidate(clause: str, antecedent: str | None) -> str | None:
    """Find a comparison, effect with a measured operand, or comparative bound to a performance quantity."""
    for pair in _CANDIDATE_PAIR.finditer(clause):
        if (
            _DURATION_VALUE.fullmatch(pair["after"])
            and _DURATION_PURPOSE.match(clause[pair.end() :])
            and not _ELAPSED_TIME.search(clause[: pair.start()])
        ):
            return None
        if metric := _pair_candidate(clause, pair):
            return metric
    for pattern in _CANDIDATE_SHAPES:
        if match := pattern.search(clause):
            slots = match.groupdict()
            return _quantity_metric(slots.get("object") or slots.get("quantity") or match[0]) or "time"
    if re.search(_NUM, clause) and (outcome := _NUMBERED_OUTCOME.search(clause)):
        return _quantity_metric(outcome["label"])
    if antecedent and (_BOUND_PRONOUN.search(clause) or _PARALLEL_DELTA.fullmatch(clause.rstrip(".!?"))):
        return antecedent
    return None


def _prose_clauses(line: str) -> Iterator[tuple[str, str]]:
    """Yield original and code-masked clauses without splitting measured comparisons."""
    masked = _INLINE_CODE.sub(lambda match: "\ufffc" + " " * (len(match[0]) - 1), line)
    comparisons = [match.span() for match in _CANDIDATE_PAIR.finditer(masked)]
    comparisons += [match.span() for match in _ATTACHED_COMPARISON.finditer(masked)]
    start = 0
    for boundary in _CLAUSE_BOUNDARY.finditer(masked):
        # Measurement syntax and leading context can include punctuation inside a single assertion.
        if any(left <= boundary.start() < right for left, right in comparisons):
            continue
        remainder = masked[start:].lstrip(" \t#*>-")
        context = _LEADING_CONTEXT.match(remainder)
        if context and boundary.start() < len(masked) - len(remainder) + context.end():
            continue
        yield line[start : boundary.start()].strip(), masked[start : boundary.start()].strip()
        start = boundary.end()
    yield line[start:].strip(), masked[start:].strip()


def _prose_claims(text: str) -> list[tuple[str, str, float | None, str | None]]:
    """Preserve a stated effect and grant direction only to a complete head assertion."""
    found = []
    for line in _lines(text):
        antecedent, head = None, True
        for original, clause in _prose_clauses(line):
            metric = _claim_candidate(clause, antecedent)
            claim, head = _line_claim(clause, antecedent, implicit=head)
            if metric:
                antecedent = claim[0] if claim else metric
                if claim is None or claim[2] is not None:
                    found.append((original, *(claim or (metric, None, None))))
    return found


def extract_claims(sources: Sequence[tuple[str, str]]) -> dict[str, object]:
    """Extract performance and memory claims from ``(source label, text)`` pairs.

    Claims use comparable before/after quantities or a directional comparison attributed to the proposed head.
    Hardware named on the claim clause or table row binds it; hardware elsewhere describes the author's environment.

    Example:
        >>> doc = extract_claims([("body", "Benchmarked on an RTX 4090.\\nThe NMS is now 3x faster for 10k boxes.")])
        >>> c = doc["claims"][0]
        >>> (c["metric"], c["claimed_relative"], c["expected_direction"], c["accelerator"], c["requires"])
        ('time', -0.6667, 'decrease', 'cuda', [])
    """
    claims: list[dict[str, object]] = []
    snippets: list[dict[str, str]] = []
    for label, text in sources:
        snippets += [{"source": label, "code": block.strip()} for block in _FENCE.findall(text)]
        for claim_text, metric, rel, direction in [*_prose_claims(text), *table_claims(text)]:
            claims.append(_claim_record(label, claim_text, metric, rel, direction, text))
    for index, claim in enumerate(claims, start=1):
        claim["id"] = f"c{index}"
    return {"schema": SCHEMA, "kind": "review-claims", "claims": claims, "author_snippets": snippets}


def read_pr_texts(
    pr_meta: Path, text_files: Sequence[Path], comments_json: Path | None = None
) -> list[tuple[str, str]]:
    """Read the PR body from the frozen snapshot, saved comments (``gh pr view --json comments``) and text files.

    Each comment is its own source, labelled ``comment:<login>``, so a claim records who made it.
    """
    meta = _load(pr_meta, "pr-meta")
    body = meta.get("body")
    sources = [("body", body)] if isinstance(body, str) and body.strip() else []
    if comments_json is not None and comments_json.is_file():
        comments = _load(comments_json, "comments").get("comments")
        for item in comments if isinstance(comments, list) else []:
            author = item.get("author") if isinstance(item, dict) else None
            login = author.get("login") if isinstance(author, dict) else None
            text = item.get("body") if isinstance(item, dict) else None
            if isinstance(text, str) and text.strip():
                sources.append((f"comment:{login or 'unknown'}", text))
    for path in text_files:
        try:
            sources.append((path.name, path.read_text(encoding="utf-8", errors="replace")))
        except OSError as exc:
            raise ClaimReproError(f"text file unreadable: {exc}") from exc
    return sources


# --------------------------------------------------------------------------------------------------------------------
# hardware decision
# --------------------------------------------------------------------------------------------------------------------


def _arch_family(arch: str | None) -> str | None:
    """Normalize an architecture name to ``x86_64`` or ``arm64``."""
    value = (arch or "").strip().lower()
    return _ARCH_FAMILIES.get(value, value) or None


def _decision(decision: str, basis: str, reason: str, requires: str | None = None) -> dict[str, object]:
    """Build one comparable-hardware record: the shared three-value ``decision`` plus this helper's ``basis``."""
    return {"decision": decision, "basis": basis, "comparable": decision != "not-comparable", "requires": requires,
            "reason": reason}  # fmt: skip


def host_class(host: dict[str, object]) -> dict[str, object]:
    """Return the host class recorded per run (from the provisioner's host spec, or derived the same way).

    Example:
        >>> host_class({"os": "Darwin", "machine": "arm64", "native_arch": "arm64", "accelerators": ["mps"]})
        {'os': 'macos', 'machine': 'arm64', 'native_arch': 'arm64', 'accelerators': ['mps']}
    """
    recorded = host.get("host_class")
    return dict(recorded) if isinstance(recorded, dict) else provision.host_class_of(host)


def _accelerator_gap(claim: dict[str, object], host: dict[str, object]) -> tuple[str, str] | None:
    """Return ``(reason, class)`` for a required accelerator class this host lacks, with its probe evidence."""
    need = claim.get("accelerator")
    if not (isinstance(need, str) and need in ACCELERATOR_NEEDS):
        return None
    satisfying = ACCELERATOR_NEEDS[need]
    if {str(c) for c in host.get("accelerators") or []}.intersection(satisfying):  # type: ignore[union-attr]
        return None
    absent = host.get("absent_accelerators") if isinstance(host.get("absent_accelerators"), dict) else {}
    evidence = "; ".join(f"{c}: {absent.get(c, 'not detected')}" for c in satisfying)  # type: ignore[union-attr]
    classes = " or ".join(ACCELERATOR_CLASS_NAMES[c] for c in satisfying)
    return f"requires {ACCELERATOR_LABELS[need]}; host has no {classes} device ({evidence})", need


def _os_gap(claim: dict[str, object], host: dict[str, object]) -> tuple[str, str] | None:
    """Return ``(reason, class)`` when the claim line names OS classes and this host is none of them."""
    wanted = [str(w) for w in claim.get("requires_os") or [] if str(w) in OS_LABELS]  # type: ignore[union-attr]
    if not wanted:
        return None
    current = str(host_class(host).get("os") or provision.host_class_of(host)["os"] or "unknown")
    if current in wanted:
        return None
    return f"requires {' or '.join(OS_LABELS[w] for w in wanted)}; host is {OS_LABELS.get(current, current)}", wanted[0]


def _arch_gap(claim: dict[str, object], host: dict[str, object]) -> tuple[str, str] | None:
    """Return ``(reason, class)`` when the claim line names CPU architectures and this host is none of them."""
    wanted = [str(w) for w in claim.get("requires_arch") or []]  # type: ignore[union-attr]
    current = _host_arch_family(host)
    if not wanted or current in wanted:
        return None
    return f"requires {' or '.join(wanted)}; host is {current or 'unknown'}", wanted[0]


def _host_arch_family(host: dict[str, object]) -> str | None:
    """Normalize the recorded native architecture, falling back to the host's machine name."""
    native = host_class(host).get("native_arch") or host.get("native_arch") or host.get("machine")
    return _arch_family(str(native)) if native else None


def _binds_accelerator(claim: dict[str, object]) -> bool:
    """Tell whether the claim's accelerator is its requirement: named on the claim line, or no ``requires`` recorded.

    An extracted claim lists only line-named classes in ``requires``; an accelerator named elsewhere in the body (a
    benchmark-machine sentence, "No GPU needed") is the author's environment.
    """
    requires = claim.get("requires")
    return requires is None or bool(requires)


def hardware_decision(claim: dict[str, object], host: dict[str, object]) -> dict[str, object]:
    """Decide whether this host can test a claim, from the claim's hardware and the host's observed facts.

    ``decision`` is ``same-class``, ``proportional`` or ``not-comparable`` (the shape the Codex Rig claim record
    shares); ``basis`` says why. The host class is a fact of this run, never a gap. Only a claim whose required
    accelerator class, OS or CPU architecture (each named on the claim line) differs from this host is
    ``not-comparable``, checked in that order, and its reason names exactly that class:
    ``not reproducible on this host: requires <CLASS>; host has no <CLASS> device (<probe evidence>)`` or
    ``…; host is <class>``. No emulation is attempted. Hardware the author names elsewhere compares the relative effect;
    an accelerator named only elsewhere that this host lacks is ``proportional`` with basis
    ``document-accelerator-absent``, and its reason travels into the verdict as a caveat.

    Examples:
        >>> host = {"os": "Darwin", "native_arch": "arm64", "accelerators": ["mps"],
        ...         "absent_accelerators": {"cuda": "nvidia-smi not on PATH"}}
        >>> hardware_decision({"accelerator": "tensorrt"}, host)["reason"].removeprefix(HOST_PREFIX)
        'requires CUDA (TensorRT); host has no CUDA device (cuda: nvidia-smi not on PATH)'
        >>> hardware_decision({"requires_os": ["windows"]}, host)["reason"]
        'not reproducible on this host: requires Windows; host is macOS'
        >>> hardware_decision({"requires_arch": ["x86_64"]}, host)["reason"]
        'not reproducible on this host: requires x86_64; host is arm64'
        >>> hardware_decision({"accelerator": None, "cpu_family": "x86_64"}, host)["decision"]
        'proportional'
        >>> hardware_decision({"accelerator": "cuda", "requires": []}, host)["basis"]
        'document-accelerator-absent'
    """
    accelerator = _accelerator_gap(claim, host)
    required_accelerator = accelerator if _binds_accelerator(claim) else None
    for basis, gap in (("accelerator-absent", required_accelerator), ("os-absent", _os_gap(claim, host)),
                       ("arch-absent", _arch_gap(claim, host))):  # fmt: skip
        if gap is not None:
            return _decision("not-comparable", basis, f"{HOST_PREFIX}{gap[0]}", gap[1])
    if accelerator is not None:
        reason = (f"the PR text names {ACCELERATOR_LABELS[accelerator[1]]} outside the claim line and this host has "
                  f"none: relative effect measured without it ({accelerator[0].split('; ', 1)[1]})")  # fmt: skip
        return _decision("proportional", "document-accelerator-absent", reason)
    need = claim.get("accelerator")
    if isinstance(need, str) and need in ACCELERATOR_NEEDS:
        classes = " or ".join(ACCELERATOR_CLASS_NAMES[c] for c in ACCELERATOR_NEEDS[need])
        return _decision("same-class", "same-accelerator-class", f"host has {classes}", need)
    host_family = _host_arch_family(host)
    author = claim.get("cpu_family")
    if not author:
        return _decision(
            "proportional",
            "author-hardware-unstated",
            f"no author CPU stated; relative effect measured on this host's {host_family} CPU",
        )
    if author == host_family:
        return _decision("same-class", "same-cpu-family", f"author and host CPU family {host_family}")
    return _decision(
        "proportional",
        "similar-class-cpu",
        f"author CPU family {author}, host {host_family}: relative effect compared, absolute figures not",
    )


# --------------------------------------------------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------------------------------------------------

HARNESS = '''"""Time one benchmark in this process and print one JSON result line (review claim reproduction harness).

The benchmark file defines bench() and may define setup(); setup's return value is passed to bench. The working
directory is the tree under test: its root and src directory go first on sys.path, so the benchmark imports that tree.
"""

import gc
import json
import os
import runpy
import statistics
import sys
import time
import tracemalloc

MARKER = "claim_repro"


def _args(argv):
    bench, reps, warmup, memory = argv[1], 20, 3, False
    rest = list(argv[2:])
    while rest:
        key = rest.pop(0)
        if key == "--reps":
            reps = int(rest.pop(0))
        elif key == "--warmup":
            warmup = int(rest.pop(0))
        elif key == "--memory":
            memory = True
    return bench, max(1, reps), max(0, warmup), memory


def _peak_rss_mb():
    # Linux keeps the spawning process's peak in ru_maxrss across exec, which hides growth below it; VmHWM is this process only
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def main():
    bench_path, reps, warmup, memory = _args(sys.argv)
    root = os.getcwd()
    for path in (root, os.path.join(root, "src")):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    namespace = runpy.run_path(bench_path, run_name="claim_repro_bench")
    bench, setup = namespace.get("bench"), namespace.get("setup")
    if not callable(bench):
        raise SystemExit("benchmark defines no bench() callable")
    state = setup() if callable(setup) else None
    call = (lambda: bench(state)) if callable(setup) else bench
    rss_growth = None
    if memory:
        # first call, before warm-up: the resident peak it adds above interpreter, imports and setup()
        gc.collect()
        before = _peak_rss_mb()
        call()
        after = _peak_rss_mb()
        rss_growth = None if before is None or after is None else max(0.0, after - before)
    for _ in range(warmup):
        call()
    times = []
    for _ in range(reps):
        gc.collect()
        start = time.perf_counter()
        call()
        times.append(time.perf_counter() - start)
    result = {MARKER: 1, "time_s": statistics.median(times), "reps": reps, "warmup": warmup}
    if memory:
        gc.collect()
        tracemalloc.start()
        call()
        result["tracemalloc_peak_mb"] = tracemalloc.get_traced_memory()[1] / (1024 * 1024)
        tracemalloc.stop()
        result["rss_growth_mb"] = rss_growth
    result["peak_rss_mb"] = _peak_rss_mb()
    sys.stdout.flush()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
'''


def _git(repo: Path, *args: str) -> str:
    """Run one git command and return stdout; raise ``ClaimReproError`` on failure."""
    try:
        done = subprocess.run([_GIT, "-C", str(repo), *args], capture_output=True, text=True, timeout=300, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClaimReproError(f"git {args[0]} failed: {exc}") from exc
    if done.returncode != 0:
        raise ClaimReproError(f"git {args[0]} failed: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def _commit(repo: Path, ref: str) -> str:
    """Resolve a ref to a full commit id; an option-shaped ref is refused."""
    if not ref or ref.startswith("-"):
        raise ClaimReproError(f"invalid ref {ref!r}")
    return _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")


def _is_inside(path: Path, parent: Path) -> bool:
    """Tell whether ``path`` resolves inside ``parent``."""
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _load(path: Path, what: str) -> dict[str, object]:
    """Read a JSON object or fail with exit status 2."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClaimReproError(f"{what} unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ClaimReproError(f"{what} is not a JSON object")
    return data


def bench_candidates(repo: Path, base: str, head: str) -> list[str]:
    """List files the PR changes that look like benchmarks (the author's own benchmark, if any)."""
    changed = _git(repo, "diff", "--name-only", f"{base}...{head}").splitlines()
    return [p for p in changed if re.search(r"(?:^|/)(?:bench(?:mark)?s?/|[^/]*bench[^/]*\.py$|asv)", p, re.I)]


def _sha256(path: Path) -> str:
    """Hash one file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class PlanInputs:
    """Paths and parameters ``plan`` works from."""

    claims: dict[str, object]
    env: dict[str, object]
    repo: Path
    base_ref: str
    head_ref: str
    work_dir: Path
    bench: Path | None
    reps: int
    warmup: int
    selected: tuple[str, ...] = ()


def _plan_status(inputs: PlanInputs, head: str) -> tuple[str, str]:
    """Return ``(status, reason)`` before any tree is prepared: ready, not-run or no-benchmark."""
    env_python = inputs.env.get("env_python")
    if not isinstance(env_python, str) or not Path(env_python).exists():
        causes = sorted({str(f.get("cause")) for f in inputs.env.get("failed") or [] if isinstance(f, dict)})  # type: ignore[union-attr]
        return "not-run", f"reviewer environment unavailable ({', '.join(causes) or 'no env_python'})"
    checkout = _git(inputs.repo, "rev-parse", "HEAD")
    if checkout != head:
        return "not-run", f"checkout at {checkout[:12]} is not the reviewed head {head[:12]}"
    dirty = _git(inputs.repo, "--no-optional-locks", "status", "--porcelain", "--untracked-files=normal")
    if dirty:
        # The active review must refer to the committed source before planning a comparison.
        return (
            "not-run",
            f"checkout of {head[:12]} has uncommitted or untracked changes ({len(dirty.splitlines())} paths)",
        )
    if inputs.bench is None:
        return "no-benchmark", "no benchmark file supplied"
    if not re.search(r"^def bench\(", inputs.bench.read_text(encoding="utf-8", errors="replace"), re.MULTILINE):
        return "no-benchmark", f"{inputs.bench.name} defines no top-level bench()"
    return "ready", ""


def build_plan(inputs: PlanInputs) -> dict[str, object]:
    """Prepare both trees, the harness and the exact per-side commands; decide hardware per claim."""
    base, head = _commit(inputs.repo, inputs.base_ref), _commit(inputs.repo, inputs.head_ref)
    toplevel = Path(_git(inputs.repo, "rev-parse", "--show-toplevel"))
    if _is_inside(inputs.work_dir, toplevel):
        raise ClaimReproError(f"--work-dir {inputs.work_dir} is inside the reviewed repository")
    host = inputs.env.get("host") if isinstance(inputs.env.get("host"), dict) else {}
    claims = [dict(c) for c in inputs.claims.get("claims") or [] if isinstance(c, dict)]  # type: ignore[union-attr]
    for claim in claims:
        claim["hardware"] = hardware_decision(claim, host)  # type: ignore[arg-type]
        claim["benchmarked"] = (
            inputs.bench is not None and claim.get("parse_status") != "unparsed" and claim.get("id") in inputs.selected
        )
    status, reason = _plan_status(inputs, head)
    if status == "ready" and not any(c["benchmarked"] for c in claims):
        status, reason = "no-benchmark", "no parsed claim IDs selected for the supplied benchmark"
    plan: dict[str, object] = {
        "schema": SCHEMA, "kind": "review-claim-plan", "status": status, "reason": reason, "base": base,
        "head": head, "host": host, "claims": claims, "candidates": bench_candidates(inputs.repo, base, head),
        "author_snippets": len(inputs.claims.get("author_snippets") or []),  # type: ignore[arg-type]
    }  # fmt: skip
    if status != "ready":
        return plan
    if not any(_can_measure(claim) for claim in claims):
        plan.update(status="not-run", reason="no selected comparable claim has a sample source")
        return plan
    plan.update(_prepare_trees(inputs, base, head, toplevel, claims))
    return plan


def _prepare_trees(
    inputs: PlanInputs, base: str, head: str, toplevel: Path, claims: list[dict[str, object]]
) -> dict[str, object]:
    """Clone both planned revisions and prepare the shared harness and benchmark outside those trees."""
    work = inputs.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    sides = {}
    for side, sha in (("base", base), ("head", head)):
        tree = work / side
        if tree.exists():
            shutil.rmtree(tree)
        _clone_at(toplevel, tree, sha)
        sides[side] = {"tree": str(tree), "sha": sha}
    harness, bench = work / "harness.py", work / "bench.py"
    harness.write_text(HARNESS, encoding="utf-8")
    shutil.copyfile(inputs.bench, bench)  # type: ignore[arg-type]
    env_python = str(inputs.env["env_python"])
    argv = [env_python, "-I", str(harness), str(bench), "--reps", str(inputs.reps), "--warmup", str(inputs.warmup)]
    if any(c.get("metric") == "memory" for c in claims):
        argv.append("--memory")
    python = inputs.env.get("python") if isinstance(inputs.env.get("python"), dict) else {}
    roots = [str(p) for p in (inputs.env.get("env_dir"), python.get("base_prefix"), work) if p]  # type: ignore[union-attr]
    return {
        "work_dir": str(work), "harness": str(harness), "harness_sha256": _sha256(harness), "bench": str(bench),
        "bench_sha256": _sha256(bench), "bind_files": [str(harness), str(bench)], "argv": argv,
        "readable_roots": roots, "env_dir": inputs.env.get("env_dir"), "env_python": env_python,
        "reps": inputs.reps, "warmup": inputs.warmup,
        "sides": sides,
    }  # fmt: skip


def _clone_at(source: Path, tree: Path, sha: str) -> None:
    """Materialize and verify a detached checkout from local Git objects."""
    if _commit(source, sha) != sha:
        raise ClaimReproError(f"benchmark revision {sha} is not a full commit ID")
    _git(tree.parent, "clone", "--shared", "--no-checkout", "--quiet", "--", str(source), str(tree))
    _git(tree, "checkout", "--detach", "--quiet", sha)
    if _git(tree, "rev-parse", "HEAD") != sha:
        raise ClaimReproError(f"benchmark checkout {tree.name} does not match planned revision {sha}")


# --------------------------------------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunMode:
    """How the gate lets the benchmark run: ``direct``, ``guard`` or ``none`` with the reason."""

    kind: str
    reason: str
    approvals: dict[str, str] = field(default_factory=dict)


def _nonempty(path: Path) -> bool:
    """Tell whether an approval file exists and holds something (the authorize block empties it before binding)."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def gate_mode(clause_file: Path, pr_meta: Path | None, approvals: dict[str, Path | None]) -> RunMode:
    """Read the review's recorded PR-code decision; nothing here can widen it.

    Examples:
        >>> gate_mode(Path("/nonexistent/code-clause.txt"), None, {}).kind
        'none'
    """
    try:
        clause = clause_file.read_text(encoding="utf-8").strip()
    except OSError:
        return RunMode("none", "no PR-code clause recorded: nothing runs")
    if clause == SAME_REPO_CLAUSE:
        try:
            meta = json.loads(pr_meta.read_text(encoding="utf-8")) if pr_meta else None
        except (OSError, ValueError):
            meta = None
        if isinstance(meta, dict) and meta.get("isCrossRepository") is False:
            return RunMode("direct", "same-repo PR: runs directly, as the review's clause records")
        return RunMode("none", "direct-run clause without a same-repo snapshot: refused")
    if _APPROVAL_CLAUSE.match(clause):
        missing = [side for side, path in approvals.items() if path is None or not _nonempty(path)]
        if missing or set(approvals) != {"base", "head"}:
            return RunMode(
                "none", f"approval clause recorded, but no bound approval for {', '.join(missing) or 'both sides'}"
            )
        return RunMode("guard", "contributor-code gate approval", {k: str(v) for k, v in approvals.items()})
    return RunMode("none", f"the review's PR-code clause forbids running PR code: {clause[:160]}")


@dataclass
class SideStats:
    """Per-side measurements across rounds."""

    times: list[float] = field(default_factory=list)
    #: Python-heap peak of one call (``tracemalloc``), for ``heap`` memory claims.
    memory: list[float] = field(default_factory=list)
    #: Resident-memory peak one call adds above the post-setup peak, for ``rss`` memory claims.
    rss: list[float] = field(default_factory=list)
    #: Total resident high-water mark for the benchmark process, including imports and setup.
    peak_rss: list[float] = field(default_factory=list)
    isolation: str = ""


def _tail(text: str) -> str:
    """Keep the last lines of a failed run's output."""
    return "\n".join([ln for ln in text.strip().splitlines() if ln.strip()][-TAIL_LINES:])


def _result_line(stdout: str) -> dict[str, object] | None:
    """Return the harness result: the last JSON line carrying the marker key (benchmark output is data)."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and RESULT_MARKER in line:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get(RESULT_MARKER) == 1:
                return data
    return None


def _side_env(plan: dict[str, object]) -> dict[str, str]:
    """Environment for one run: the review env's interpreter and readable roots on ``PATH``, a fixed hash seed.

    The guard's sandbox makes only its interpreter prefixes, ``VIRTUAL_ENV`` and ``PATH`` entries readable, so the
    environment, its base interpreter and the work directory are put there; the guard still clears everything else.
    """
    env = dict(os.environ)
    env_dir = str(plan.get("env_dir") or "")
    bin_dir = str(Path(str(plan["env_python"])).parent)
    roots = [bin_dir, *[str(r) for r in plan.get("readable_roots") or []]]  # type: ignore[union-attr]
    env["PATH"] = os.pathsep.join([*roots, env.get("PATH", "")])
    if env_dir:
        env["VIRTUAL_ENV"] = env_dir
    env["PYTHONHASHSEED"] = "0"
    return env


def invoke(argv: Sequence[str], env: dict[str, str], cwd: str, timeout: int) -> tuple[int, str, str]:
    """Run one process and return ``(exit status, stdout, stderr)``; a timeout is exit 124."""
    try:
        done = subprocess.run(  # noqa: S603 - argv list from the plan, no shell; PR code reaches it only via the guard
            list(argv), env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 127, "", f"cannot start {argv[0]}: {exc}"
    return done.returncode, done.stdout, done.stderr


def _run_side(
    plan: dict[str, object], side: str, mode: RunMode, timeout: int
) -> tuple[dict[str, object] | None, str, str]:
    """Run the benchmark once for one side; return ``(result, isolation line, failure reason)``."""
    tree = str(plan["sides"][side]["tree"])  # type: ignore[index]
    argv = [str(a) for a in plan["argv"]]  # type: ignore[union-attr]
    env = _side_env(plan)
    if mode.kind == "guard":
        cmd = [sys.executable, "-I", str(GUARD), "run", "--approval-file", mode.approvals[side], "--repo", tree,
               "--head", str(plan[side]), "--timeout", str(timeout), "--", *argv]  # fmt: skip
        rc, out, err = invoke(cmd, env, tree, timeout + 120)
    else:
        rc, out, err = invoke(argv, env, tree, timeout)
    isolation = next((ln.strip() for ln in err.splitlines() if ln.startswith("untrusted-guard: isolation=")), "")
    if rc in (3, 4) and mode.kind == "guard":
        return None, isolation, f"contributor-code gate refused the {side} run (exit {rc}): {_tail(err)}"
    result = _result_line(out) if rc == 0 else None
    if result is None:
        return None, isolation, f"{side} benchmark failed (exit {rc}): {_tail(err or out)}"
    return result, isolation, ""


def measure(
    plan: dict[str, object], mode: RunMode, rounds: int, timeout: int, budget: int = DEFAULT_BUDGET
) -> tuple[dict[str, SideStats], str]:
    """Run ABBA rounds within ``budget`` seconds; return per-side stats and the failure reason (empty when usable).

    A budget stop keeps the complete rounds when there are at least ``MIN_ROUNDS``; fewer is a failure, so a caller's
    tool cap never kills the run before it can write its result.
    """
    if mode.kind != "direct":
        return _measure_rounds(plan, mode, rounds, timeout, budget)
    try:
        with tempfile.TemporaryDirectory(prefix="direct-", dir=str(plan["work_dir"])) as scratch:
            sides = {}
            for side in ("base", "head"):
                source = Path(plan["sides"][side]["tree"])  # type: ignore[index]
                tree, sha = Path(scratch) / side, str(plan[side])
                _clone_at(source, tree, sha)
                sides[side] = {"tree": str(tree), "sha": sha}
            return _measure_rounds({**plan, "sides": sides}, mode, rounds, timeout, budget)
    except (ClaimReproError, OSError) as exc:
        return {"base": SideStats(), "head": SideStats()}, f"could not verify planned benchmark revisions: {exc}"


def _measure_rounds(
    plan: dict[str, object], mode: RunMode, rounds: int, timeout: int, budget: int
) -> tuple[dict[str, SideStats], str]:
    """Collect complete alternating rounds from the trees selected for the execution mode."""
    stats = {"base": SideStats(), "head": SideStats()}
    deadline = time.monotonic() + budget
    for index in range(rounds):
        for side in ("base", "head") if index % 2 == 0 else ("head", "base"):
            remaining = int(deadline - time.monotonic())
            if remaining < 1:
                return _budget_stop(stats, budget)
            result, isolation, failure = _run_side(plan, side, mode, min(timeout, remaining))
            if failure:
                return stats, failure
            _record_sample(stats[side], result, isolation)  # type: ignore[arg-type]
    return stats, ""


def _record_sample(side: SideStats, result: dict[str, object], isolation: str) -> None:
    """Add one harness result to a side: its time, and each memory reading the harness could take."""
    side.isolation = side.isolation or isolation
    side.times.append(float(result["time_s"]))  # type: ignore[arg-type]
    for key, samples in (
        ("tracemalloc_peak_mb", side.memory),
        ("rss_growth_mb", side.rss),
        ("peak_rss_mb", side.peak_rss),
    ):
        if result.get(key) is not None:
            samples.append(float(result[key]))  # type: ignore[arg-type]


def _budget_stop(stats: dict[str, SideStats], budget: int) -> tuple[dict[str, SideStats], str]:
    """Keep only complete rounds after a budget stop; fewer than ``MIN_ROUNDS`` is a failure."""
    complete = min(len(stats["base"].times), len(stats["head"].times))
    for side in stats.values():
        for samples in (side.times, side.memory, side.rss, side.peak_rss):
            del samples[complete:]
    if complete >= MIN_ROUNDS:
        return stats, ""
    return stats, f"run budget of {budget}s exhausted after {complete} complete round(s)"


def _summary(values: Sequence[float]) -> tuple[float | None, float]:
    """Return ``(median, interquartile range)`` across rounds, the range in the metric's own unit.

    Quartiles use ``statistics.quantiles(n=4, method="inclusive")``, the statistic the Codex Rig claim record shares.

    Example:
        >>> _summary([1.0, 2.0, 3.0, 4.0, 5.0])
        (3.0, 2.0)
    """
    if not values:
        return None, 0.0
    if len(values) < 2:
        return statistics.median(values), 0.0
    q1, _, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return statistics.median(values), q3 - q1


def judge(claim: dict[str, object], base: float, head: float, spread: float) -> tuple[Verdict, str, float]:
    """Compare the measured relative effect with the claim; return ``(verdict, reason, relative delta)``.

    Examples:
        >>> judge({"expected_direction": "decrease", "claimed_relative": -0.5}, 1.0, 0.55, 0.05)[0]
        <Verdict.REPRODUCED: 'reproduced'>
        >>> judge({"expected_direction": "decrease", "claimed_relative": -0.5}, 1.0, 0.9, 0.05)[0]
        <Verdict.NOT_REPRODUCED: 'not-reproduced'>
        >>> judge({"expected_direction": "decrease", "claimed_relative": None}, 1.0, 0.99, 0.05)[1]
        'measured -1.0% is within the noise band ±5.0%'
    """
    delta = (head - base) / base
    noise = max(MIN_EFFECT, spread)
    claimed = claim.get("claimed_relative")
    claimed_text = f"{float(claimed):+.1%}" if isinstance(claimed, (int, float)) else "no stated magnitude"
    if abs(delta) <= noise:
        return Verdict.NOT_REPRODUCED, f"measured {delta:+.1%} is within the noise band ±{noise:.1%}", delta
    if (delta < 0) != (claim.get("expected_direction") == "decrease"):
        return (
            Verdict.NOT_REPRODUCED,
            f"measured {delta:+.1%}, opposite to the claimed direction ({claimed_text})",
            delta,
        )
    if isinstance(claimed, (int, float)) and abs(delta) < CLAIM_FRACTION * abs(claimed):
        return (Verdict.NOT_REPRODUCED,
                f"measured {delta:+.1%} is under half the claimed {claimed_text} (noise ±{noise:.1%})", delta)  # fmt: skip
    return (
        Verdict.REPRODUCED,
        f"measured {delta:+.1%} in the claimed direction ({claimed_text}), noise ±{noise:.1%}",
        delta,
    )


def _record(claim: dict[str, object], host: object, method: dict[str, object]) -> dict[str, object]:
    """Start one output record with the fields every verdict carries."""
    hardware = claim.get("hardware") if isinstance(claim.get("hardware"), dict) else {}
    host_spec = {**host, "host_class": host_class(host)} if isinstance(host, dict) else host
    return {
        "id": claim.get("id"), "claim_text": claim.get("claim_text"), "metric": claim.get("metric"),
        "author_hw": claim.get("author_hw"), "host_spec": host_spec, "method": method,
        "requires": claim.get("requires", []), "requires_os": claim.get("requires_os", []),
        "requires_arch": claim.get("requires_arch", []), "author_os": claim.get("author_os"),
        "comparable_hardware": hardware, "claimed_relative": claim.get("claimed_relative"),
        "direction": None if claim.get("parse_status") == "unparsed" else "lower-is-better",
        "expected_direction": claim.get("expected_direction"),
        "base_samples_n": 0, "head_samples_n": 0,
        "base_median": None, "head_median": None, "relative_delta": None, "spread": None,
    }  # fmt: skip


def evaluate(plan: dict[str, object], mode: RunMode, stats: dict[str, SideStats] | None, failure: str,
             method: dict[str, object]) -> list[dict[str, object]]:  # fmt: skip
    """Turn the plan, gate mode and measurements into one record per claim."""
    records = []
    for claim in plan.get("claims") or []:  # type: ignore[union-attr]
        record = _record(claim, plan.get("host"), method)
        hardware = record["comparable_hardware"]
        if claim.get("parse_status") == "unparsed":
            record.update(parse_status="unparsed", verdict=Verdict.NOT_RUN.value, reason=_UNPARSED_REASON)
        elif not hardware.get("comparable", False):  # type: ignore[union-attr]
            record.update(verdict=Verdict.HARDWARE_UNAVAILABLE.value, reason=hardware.get("reason"))  # type: ignore[union-attr]
        elif plan.get("status") == "no-benchmark":
            record.update(verdict=Verdict.NO_BENCHMARK.value, reason=plan.get("reason"))
        elif claim.get("benchmarked") is not True:
            targets = ", ".join(str(c.get("id")) for c in plan.get("claims") or [] if c.get("benchmarked"))  # type: ignore[union-attr]
            reason = f"the supplied benchmark measures {targets} only" if targets else "no explicit benchmark coverage"
            record.update(verdict=Verdict.NO_BENCHMARK.value, reason=reason)
        elif (source := _samples_for(claim))[0] is None:
            record.update(verdict=Verdict.NOT_RUN.value, reason=f"{source[1]} is not measured by this harness")
        elif plan.get("status") != "ready" or mode.kind == "none" or failure or stats is None:
            reason = (
                plan.get("reason")
                if plan.get("status") != "ready"
                else (mode.reason if mode.kind == "none" else failure)
            )
            record.update(verdict=Verdict.NOT_RUN.value, reason=reason)
        else:
            record.update(_measured(claim, stats, source[0], source[1]))
        records.append(record)
    return records


#: Per memory kind: the ``SideStats`` samples that measure it and the words a ``not-run`` reason names them by.
_MEMORY_SAMPLES = {
    "heap": ("memory", "Python-heap (tracemalloc)"),
    "rss": ("rss", "process RSS growth (peak resident memory one call adds; not measurable on this host when absent)"),
    "peak-rss": ("peak_rss", "total process peak RSS"),
    "allocation-count": (None, "allocation count"),
    "device": (None, "device memory"),
}
_NON_MEDIAN_TIMING = re.compile(r"\b(?:p\d{1,2}(?:\.\d+)?|percentile|mean|average|avg)\b", re.I)


def _samples_for(claim: dict[str, object]) -> tuple[str | None, str]:
    """Return the sample field, if available, and the quantity it describes.

    The harness measures neither device memory nor allocation counts. Old plans can label counts as heap.
    """
    metric = claim.get("metric")
    text = str(claim.get("claim_text") or "")
    if metric in {"time", "throughput"}:
        if _NON_MEDIAN_TIMING.search(text):
            return None, "the stated timing statistic"
        return "times", "time per benchmark call"
    if metric != "memory":
        return None, "claimed quantity"
    text_kind = _memory_kind(text)
    kind = text_kind if text_kind in {"allocation-count", "peak-rss"} else str(claim.get("memory_kind") or text_kind)
    return _MEMORY_SAMPLES.get(kind, (None, "claimed memory"))


def _can_measure(claim: dict[str, object]) -> bool:
    """Require a parsed, explicitly covered claim, suitable hardware and a matching sample source."""
    return (
        claim.get("parse_status") != "unparsed"
        and claim.get("benchmarked") is True
        and claim.get("hardware", {}).get("comparable")  # type: ignore[union-attr]
        and _samples_for(claim)[0] is not None
    )


def _measured(claim: dict[str, object], stats: dict[str, SideStats], key: str, quantity: str) -> dict[str, object]:
    """Fill the measured fields and verdict for one claim, from the samples of the quantity the claim names."""
    base_values, head_values = getattr(stats["base"], key), getattr(stats["head"], key)
    counts = {"base_samples_n": len(base_values), "head_samples_n": len(head_values)}
    base_med, base_iqr = _summary(base_values)
    head_med, head_iqr = _summary(head_values)
    if base_med is None or head_med is None or not base_med:
        return {**counts, "verdict": Verdict.NOT_RUN.value, "reason": f"no {quantity} measurement recorded"}
    if min(counts.values()) < MIN_ROUNDS:
        return {**counts, "verdict": Verdict.NOT_RUN.value,
                "reason": f"fewer than {MIN_ROUNDS} samples per side; no verdict drawn"}  # fmt: skip
    # Both IQRs in units of the base median, the denominator of the relative delta: a fast head's tiny median must
    # not inflate the noise band (an IQR over its own median would; measured 216% on a real -100% effect).
    spread = max(base_iqr, head_iqr) / base_med
    verdict, reason, delta = judge(claim, base_med, head_med, spread)
    hardware = claim.get("hardware") if isinstance(claim.get("hardware"), dict) else {}
    if hardware.get("basis") == "document-accelerator-absent":  # type: ignore[union-attr]
        reason = f"{reason}; caveat: {hardware.get('reason')}"  # type: ignore[union-attr]
    return {**counts, "base_median": base_med, "head_median": head_med, "relative_delta": round(delta, 4),
            "spread": round(spread, 4), "verdict": verdict.value, "reason": reason}  # fmt: skip


def run_claims(
    plan: dict[str, object], mode: RunMode, rounds: int, timeout: int, budget: int = DEFAULT_BUDGET
) -> dict[str, object]:
    """Measure (when the plan and the gate allow it) and return the results document."""
    stats: dict[str, SideStats] | None = None
    failure = ""
    runnable = plan.get("status") == "ready" and mode.kind != "none"
    if runnable and any(_can_measure(claim) for claim in plan.get("claims") or []):  # type: ignore[union-attr]
        stats, failure = measure(plan, mode, rounds, timeout, budget)
    isolation = stats["head"].isolation if stats else ""
    method = {
        "command": plan.get("argv"), "repeats": rounds, "warmup": plan.get("warmup"), "order": "alternating",
        "order_detail": "ABBA base/head processes, one sample per side per round", "harness_reps": plan.get("reps"),
        "benchmark": plan.get("bench"), "benchmark_sha256": plan.get("bench_sha256"),
        "harness": "median of reps per process (perf_counter); memory readings are first-call RSS growth above the "
        "post-setup peak, total process peak RSS, and the tracemalloc peak of one extra call",
        "execution": mode.kind, "gate": mode.reason,
        "isolation": isolation or ("direct, same-repo" if mode.kind == "direct" else ""),
        "base": plan.get("base"), "head": plan.get("head"), "env_python": plan.get("env_python"),
    }  # fmt: skip
    host = dict(plan.get("host")) if isinstance(plan.get("host"), dict) else {}  # type: ignore[arg-type]
    host["host_class"] = host_class(host)
    return {"schema": SCHEMA, "kind": "review-claim-repro", "host": host, "host_class": host["host_class"],
            "method": method,
            "rounds_completed": min(len(stats["base"].times), len(stats["head"].times)) if stats else 0,
            "claims": evaluate(plan, mode, stats, failure, method)}  # fmt: skip


# --------------------------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    """Build the three-subcommand command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    ext = sub.add_parser("extract", help="extract claims from the PR body and saved comments")
    ext.add_argument("--pr-meta", required=True)
    ext.add_argument("--text-file", action="append", default=[])
    ext.add_argument("--comments-json", help="saved `gh pr view --json comments` output; absent file is skipped")
    pln = sub.add_parser("plan", help="prepare trees, harness and commands; decide hardware per claim")
    for name in ("--claims", "--env", "--repo", "--base", "--head", "--work-dir"):
        pln.add_argument(name, required=True)
    pln.add_argument("--bench")
    pln.add_argument("--reps", type=int, default=DEFAULT_REPS)
    pln.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    pln.add_argument("--claim", action="append", default=[], help="claim id the benchmark measures (default: none)")
    run = sub.add_parser("run", help="run base and head as the review's PR-code gate allows")
    run.add_argument("--plan", required=True)
    run.add_argument("--clause-file", required=True)
    run.add_argument("--pr-meta")
    run.add_argument("--head-approval")
    run.add_argument("--base-approval")
    run.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    run.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="seconds for all rounds together")
    for p in (ext, pln, run):
        p.add_argument("--out")
    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, object]:
    """Run one subcommand and return its JSON document."""
    if args.mode == "extract":
        comments = Path(args.comments_json) if args.comments_json else None
        return extract_claims(read_pr_texts(Path(args.pr_meta), [Path(p) for p in args.text_file], comments))
    if args.mode == "plan":
        bench = Path(args.bench).resolve() if args.bench else None
        if bench is not None and not bench.is_file():
            raise ClaimReproError(f"--bench {args.bench} is not a file")
        inputs = PlanInputs(
            _load(Path(args.claims), "claims"),
            _load(Path(args.env), "env"),
            Path(args.repo).resolve(),
            args.base,
            args.head,
            Path(args.work_dir),
            bench,
            max(1, args.reps),
            max(0, args.warmup),
            tuple(args.claim),
        )
        return build_plan(inputs)  # fmt: skip
    plan = _load(Path(args.plan), "plan")
    approvals = {"head": Path(args.head_approval) if args.head_approval else None,
                 "base": Path(args.base_approval) if args.base_approval else None}  # fmt: skip
    mode = gate_mode(Path(args.clause_file), Path(args.pr_meta) if args.pr_meta else None, approvals)
    return run_claims(plan, mode, max(1, args.rounds), max(1, args.timeout), max(1, args.budget))


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand and print its JSON document; exit 2 on bad input."""
    args = _parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(newline="\n")  # JSON consumers compare bytes; Windows text mode would add CR
    try:
        document = _dispatch(args)
    except ClaimReproError as exc:
        print(json.dumps({"schema": SCHEMA, "error": str(exc)}))
        return 2
    text = json.dumps(document, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
