"""Contract tests for review_claim_repro.py: author claims reproduced base against head, only as the gate allows.

Reviews used to list author benchmark tables as "unverified". The helper now extracts each performance or memory claim,
decides from observed host facts whether this hardware class can test it, and runs base and head in alternating
processes through the review's contributor-code gate. These tests pin the extraction, the hardware decision (a missing
accelerator is reported exactly, nothing broader), the gate reading (no option skips it), the verdict mapping (a run
that did not happen is never ``not-reproduced``) and one real direct-mode measurement.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path

import pytest
import review_claim_repro as rcr
from _process import requires_git
from _review_repo_builder import commit, git, init_repo, pyproject

_HOST_NO_CUDA = {
    "os": "Darwin", "native_arch": "arm64", "accelerators": ["mps"],
    "absent_accelerators": {"cuda": "nvidia-smi not on PATH", "rocm": "rocm-smi not on PATH"},
}  # fmt: skip
_APPROVAL_CLAUSE = (
    'PR code: run every command that loads or executes PR code only as python -I "/g/untrusted_code_guard.py" run '
    '--approval-file "/r/code-approval.txt" -- <command>, adding -p no:xdist to a pytest command; a refusal (exit 3 '
    "or 4) means it did not run: report a gap, never run it bare."
)
_REFUSAL_CLAUSE = (
    "PR code: run no command that loads or executes PR code (a test, a probe, an overlay run, a type checker or linter "
    "that loads project plugins, a build or a dependency export); report an execution gap."
)


def _claim(text: str) -> dict[str, object]:
    """Extract the single claim from one line of PR text."""
    claims = rcr.extract_claims([("body", text)])["claims"]
    assert len(claims) == 1, claims  # type: ignore[arg-type]
    return claims[0]  # type: ignore[index]


@pytest.mark.parametrize(
    ("text", "metric", "relative", "direction"),
    [
        pytest.param("NMS is now 3x faster on 10k boxes.", "time", -0.6667, "decrease", id="speedup-factor"),
        pytest.param("This patch uses 40% less memory during export.", "memory", -0.4, "decrease", id="memory-pct"),
        pytest.param("Latency went from 120 ms -> 80 ms.", "time", -0.3333, "decrease", id="before-after"),
        pytest.param("Throughput is now 25% higher (fps).", "throughput", -0.2, "decrease", id="rate-as-time"),
        pytest.param("RSS goes from 1.2 GB to 800 MB.", "memory", -0.3333, "decrease", id="unit-conversion"),
        pytest.param("Inference is faster now.", "time", None, "decrease", id="direction-only"),
        pytest.param("Latency drops from 1,200 ms -> 800 ms.", "time", -0.3333, "decrease", id="digit-grouping"),
        pytest.param("Load time goes from 2 s -> 500 ms.", "time", -0.75, "decrease", id="mixed-time-units"),
        pytest.param("Improves throughput: per-batch latency 120 ms -> 80 ms.", "time", -0.3333, "decrease",
                     id="time-units-are-not-inverted-as-a-rate"),
        pytest.param("Gives 20% lower throughput on large batches.", "throughput", 0.25, "increase",
                     id="lower-throughput-is-more-time"),
        pytest.param("Gives 20% fewer fps on large batches.", "throughput", 0.25, "increase", id="fewer-fps"),
        pytest.param("Uses 30% less memory.", "memory", -0.3, "decrease", id="less-memory"),
        pytest.param("Throughput is lower now.", "throughput", None, "increase", id="direction-only-rate-down"),
        pytest.param("Reduces latency by 30%.", "time", -0.3, "decrease", id="verb-reduces-by"),
        pytest.param("Cuts peak memory by 40%.", "memory", -0.4, "decrease", id="verb-cuts-by"),
        pytest.param("Decreases runtime by 25%.", "time", -0.25, "decrease", id="verb-decreases-by"),
        pytest.param("Increases throughput by 25%.", "throughput", -0.2, "decrease", id="verb-increases-rate"),
        pytest.param("Reduces throughput by 20% in exchange for safety.", "throughput", 0.25, "increase",
                     id="verb-reduces-rate"),
        pytest.param("Increases peak memory by 10%.", "memory", 0.1, "increase", id="verb-increases-memory"),
        pytest.param("Speeds up decoding by 50%.", "time", -0.3333, "decrease", id="verb-speeds-up-by"),
    ],
)  # fmt: skip
def test_claim_extraction(text: str, metric: str, relative: float | None, direction: str) -> None:
    """Each claim shape yields the measured quantity, its claimed relative change and the expected direction.

    Throughput claims are converted to time per call, because the harness measures time. The relative effect and its
    direction come from the stated quantities after unit normalization: grouped digits are one number, a before/after
    pair in time units is a time claim whatever the sentence calls it, and a falling rate means more time per call.
    """
    claim = _claim(text)
    assert (claim["metric"], claim["claimed_relative"], claim["expected_direction"]) == (metric, relative, direction)


@pytest.mark.parametrize(
    ("text", "metric", "relative", "direction"),
    [
        pytest.param("This reduces latency for large batches.", "time", None, "decrease", id="quantity-decrease"),
        pytest.param("Speeds up loading of big files.", "time", None, "decrease", id="speed-verb"),
        pytest.param("Cuts memory use of the cache.", "memory", None, "decrease", id="memory-verb"),
        pytest.param("Improves throughput significantly.", "throughput", None, "decrease", id="rate-improvement"),
        pytest.param("The change increases runtime for small requests.", "time", None, "increase", id="quantity-increase"),
        pytest.param("Throughput improves from 100 img/s to 150 img/s.", "throughput", -0.3333, "decrease", id="rate-from-to"),
        pytest.param("Throughput 100 fps -> 150 fps.", "throughput", -0.3333, "decrease", id="rate-arrow"),
        pytest.param("takes 10 seconds -> 500 milliseconds", "time", -0.95, "decrease", id="time-unit-aliases"),
        pytest.param("Reduces startup time by 0.5 s.", "time", None, "decrease", id="absolute-delta"),
        pytest.param("Peak memory 1 GB -> 0 GB.", "memory", None, "decrease", id="zero-memory"),
        pytest.param("Throughput 2 fps -> 0 fps.", "throughput", None, "increase", id="zero-rate"),
        pytest.param("Time goes from 0 s to 2 s.", "time", None, "increase", id="zero-baseline"),
    ],
)  # fmt: skip
def test_claim_direction_and_magnitude_follow_the_stated_quantities(text, metric, relative, direction) -> None:
    """Extract the effect even when only its direction is measurable from the text."""
    claim = _claim(text)
    assert (claim["metric"], claim["claimed_relative"], claim["expected_direction"]) == (metric, relative, direction)


@pytest.mark.parametrize(
    "text",
    [
        "Peak memory fell from 2 GB to 2 GB.",
        "| case | before (ms) | after (ms) |\n|---|---|---|\n| load | 10 | 10 |",
        "| case | peak (MB) |\n|---|---|\n| load | 10 to 10 |",
    ],
)
def test_equal_quantities_supply_no_directional_claim(text: str) -> None:
    """A neutral pair cannot send the benchmark after a directional head claim."""
    assert rcr.extract_claims([("body", text)])["claims"] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("This reduces latency for large batches.", [("time", None, "decrease")], id="latency-verb"),
        pytest.param("Speeds up loading of big files.", [("time", None, "decrease")], id="speed-verb"),
        pytest.param("Cuts memory use of the cache.", [("memory", None, "decrease")], id="memory-verb"),
        pytest.param("Throughput improves significantly.", [("throughput", None, None)], id="metric-subject-without-transition"),
        pytest.param("Throughput improves from 100 img/s to 150 img/s.", [("throughput", -0.3333, "decrease")], id="rate-pair"),
        pytest.param("Throughput 100 fps -> 150 fps.", [("throughput", -0.3333, "decrease")], id="rate-arrow"),
        pytest.param("takes 10 seconds -> 5 seconds", [("time", -0.5, "decrease")], id="duration-units"),
        pytest.param("Reduces startup time by 0.5 s.", [("time", None, "decrease")], id="absolute-delta"),
        pytest.param("NMS runtime is now 2.5 ms instead of 10 ms.", [("time", None, None)], id="instead-of"),
        pytest.param("Latency was 120 ms before this change and is 80 ms now.", [("time", -0.3333, "decrease")], id="before-and-now"),
        pytest.param("35% speed improvement on large inputs.", [("time", None, None)], id="percentage-before-noun"),
        pytest.param("Speed improvement of 35% on large inputs.", [("time", None, None)], id="percentage-after-noun"),
        pytest.param("Loading takes half the time.", [("time", None, None)], id="number-as-word"),
        pytest.param("Throughput: 1.2k img/s -> 1.8k img/s", [("throughput", None, None)], id="scaled-rate"),
        pytest.param("This PR lowers Linux latency by 20%, while Windows sees a 10% increase.",
                     [("time", -0.2, "decrease"), ("time", None, None)], id="os-contrast"),
        pytest.param("Upgrades torch from 1.13 to 2.1 for faster loading.", [("time", None, None)], id="version-pair"),
        pytest.param("Moves the cache from 2 to 3 levels, making lookups faster.", [("time", None, "decrease")], id="level-pair"),
        pytest.param("This PR reduces latency by 20% when the batch size increases from 16 to 32.",
                     [("time", -0.2, "decrease")], id="percent-beats-unrelated-pair"),
        pytest.param("Bumps batch size from 32 to 64 to improve throughput.", [("throughput", None, None)], id="size-pair"),
        pytest.param("Lowers the default timeout from 30 s to 10 s.", [("time", None, None)], id="configured-duration"),
        pytest.param("This PR reduces latency by 20% compared with the previous release, where latency rose from 50 ms to 100 ms.",
                     [("time", -0.2, "decrease")], id="historical-comparator-pair"),
        pytest.param("Latency drops from 10 to 8.", [("time", -0.2, "decrease")], id="measured-unitless-pair"),
        pytest.param("Latency drops from 8 ms to 10 ms.", [("time", None, "decrease")], id="contradictory-pair"),
        pytest.param("Latency drops by 20% from 10 ms to 5 ms.", [("time", -0.2, "decrease")], id="explicit-percent"),
        pytest.param("NMS now runs 2x faster, from 10 ms to 2 ms.", [("time", -0.5, "decrease")], id="explicit-speedup"),
        pytest.param("Fixes a regression where latency increased by 30%.", [("time", None, None)], id="fixed-regression-noun"),
        pytest.param("This fixes the slowdown introduced in 1.2.", [("time", None, None)], id="fixed-slowdown-noun"),
        pytest.param("This resolves the memory leak introduced in 1.2.", [("memory", None, None)], id="fixed-leak-noun"),
        pytest.param("This PR fixes a slowdown and cuts latency by 30%.",
                     [("time", None, None), ("time", -0.3, "decrease")], id="effect-after-slowdown"),
        pytest.param("Fixes the 2x slowdown from 1.3.", [("time", None, None)], id="historical-factor"),
        pytest.param("This PR fixes a memory leak that grew RSS by 1 GB per epoch.", [("memory", None, None)], id="historical-memory-leak"),
        pytest.param("Throughput previously dropped from 100 img/s to 50 img/s.", [("throughput", None, None)], id="historical-rate"),
        pytest.param("The old decoder used to be 2x faster.", [("time", None, None)], id="historical-speed"),
        pytest.param("Previously latency increased by 30%, but this PR reduces it by 20%.",
                     [("time", None, None), ("time", -0.2, "decrease")], id="history-and-pr-pronoun"),
        pytest.param("This should not make anything slower.", [("time", None, None)], id="negated-effect"),
        pytest.param("This PR is no longer slower than main and now runs faster.",
                     [("time", None, None), ("time", None, None)], id="negation-in-coordinated-assertion"),
        pytest.param("Latency never increases by 20%.", [("time", None, None)], id="negated-percent"),
        pytest.param("The new loader runs without slowing down.", [("time", None, None)], id="negated-speed-verb"),
        pytest.param("Without caching, latency drops by 20%.", [("time", None, None)], id="conditional-quantity"),
        pytest.param("Adds more documentation about runtime options.", [], id="unrelated-comparative"),
        pytest.param("This PR cuts latency by 20% compared with what we previously shipped.",
                     [("time", -0.2, "decrease")], id="explicit-head-comparison"),
        pytest.param("Compared to the previous release, our implementation runs 2x faster.",
                     [("time", None, None)], id="unattributed-opening-comparator"),
        pytest.param("Compared to the previous release, the patch runs 2x faster.",
                     [("time", -0.5, "decrease")], id="head-opening-comparator"),
        pytest.param("Latency isn't 2x faster.", [("time", None, None)], id="contracted-negation"),
        pytest.param("No 2x speedup should be expected.", [("time", None, None)], id="negated-factor"),
        pytest.param("Uses 2x faster CPUs.", [("time", None, None)], id="hardware-comparison"),
        pytest.param("Latency from 4 to 8 MiB.", [("time", None, None)], id="conflicting-units-and-quantity"),
        pytest.param("Peak memory fell from 8 to 4 ms.", [("memory", None, "decrease")], id="conflicting-units-and-effect"),
        pytest.param("Throughput is worse.", [("throughput", None, None)], id="unmodeled-comparative"),
        pytest.param("Loading now takes one third of the time.", [("time", None, None)], id="written-fraction"),
        pytest.param("More memory per worker.", [("memory", None, "increase")], id="per-worker-scope"),
        pytest.param("Faster load times for small configs.", [("time", None, "decrease")], id="plural-quantity"),
        pytest.param("Tested with RAM from 8 GB to 64 GB.", [("memory", None, None)], id="tested-ram-range"),
        pytest.param("Memory from 1 GB to 64 GB is supported.", [("memory", None, None)], id="supported-memory-range"),
        pytest.param("- From 10 ms to 50 ms polling interval is configurable.", [("time", None, None)], id="configurable-interval-range"),
        pytest.param("Runtime 1 s to 2 s, depending on input size.", [("time", None, None)], id="input-dependent-runtime-range"),
        pytest.param("The supported timeout duration is 10 to 30 seconds.", [("time", None, None)], id="shared-unit-timeout-range"),
        pytest.param("The old implementation is slower than this one.", [("time", None, None)], id="old-subject"),
        pytest.param("Users reported that training became 40% slower after 2.0.", [("time", None, None)], id="reported-past-speed"),
        pytest.param("The latency increased in 1.2 and this PR reverts it.", [("time", None, None)], id="past-before-revert"),
        pytest.param("In v1.4, latency dropped from 120 ms to 80 ms.", [("time", None, None)], id="past-release-transition"),
        pytest.param("This PR increases latency by 20%, and RapidJSON is 2x faster than before.",
                     [("time", 0.2, "increase"), ("time", None, None)], id="pr-effect-beats-other-subject"),
        pytest.param("RapidJSON is now 2x faster, and this change increases latency.",
                     [("time", None, None), ("time", None, "increase")], id="explicit-head-beats-current-other-subject"),
        pytest.param("This PR fixes a slowdown and cuts memory from 2 GB to 1 GB.",
                     [("time", None, None), ("memory", -0.5, "decrease")], id="coordinated-head-transition"),
        pytest.param("This PR is not now 2x faster.", [("time", None, None)], id="negated-current-factor"),
        pytest.param("Increase the timeout to 60 s because CI is slower than local.", [("time", None, None)], id="comparison-in-reason"),
        pytest.param("Numpy is faster than pure Python here", [("time", None, None)], id="other-system"),
        pytest.param("Speeds up nothing", [], id="no-measured-operation"),
        pytest.param("Retries the request up to 3 times.", [], id="retry-count"),
        pytest.param("This test retries 3 times.", [], id="subject-and-count"),
        pytest.param("Adds a `timeout` parameter (default 30 s).", [], id="duration-default"),
        pytest.param("This PR adds support for time series forecasting with 3 new models.", [], id="time-series-compound"),
        pytest.param("time series forecasting with 3 new models", [], id="time-series-label"),
        pytest.param("The memory module was refactored into 2 files.", [], id="memory-module-compound"),
        pytest.param("Adds 20% more memory modules.", [], id="percentage-on-compound"),
        pytest.param("Removes the deprecated `speed` argument from 3 functions.", [], id="speed-argument-compound"),
        pytest.param("Fixes the time display in the progress bar for runs longer than 24 hours.", [], id="time-display-compound"),
        pytest.param("The download is 1.5 GB.", [], id="download-size"),
        pytest.param("2x faster", [("time", -0.5, "decrease")], id="standalone-factor"),
        pytest.param("Reduces latency by 30%.", [("time", -0.3, "decrease")], id="latency-reduction"),
        pytest.param("Cuts peak memory by 40%.", [("memory", -0.4, "decrease")], id="peak-memory-reduction"),
        pytest.param("Increases latency by 10%.", [("time", 0.1, "increase")], id="latency-increase"),
        pytest.param("Latency 1.2s -> 800ms", [("time", -0.3333, "decrease")], id="compact-unit-arrow"),
        pytest.param("Memory drops from 1,024 MB to 512 MB.", [("memory", -0.5, "decrease")], id="grouped-memory-transition"),
        pytest.param("Speeds up inference by 30%.", [("time", -0.2308, "decrease")], id="inference-speed-percent"),
        pytest.param("Improves throughput by 30%.", [("throughput", -0.2308, "decrease")], id="throughput-percent"),
        pytest.param("Lower memory: 1 GB -> 0 GB", [("memory", None, "decrease")], id="zero-memory-arrow"),
        pytest.param("This adds 20% more coverage.", [], id="coverage-percent"),
        pytest.param("Updated the docs and fixed a typo.", [], id="docs-only"),
        pytest.param("Makes NMS 2x faster", [("time", -0.5, "decrease")], id="head-summary-factor"),
        pytest.param("Latency before/after: 100 ms to 50 ms.", [("time", -0.5, "decrease")], id="before-after-labels"),
        pytest.param("Before: 100 ms; after: 50 ms.", [("time", -0.5, "decrease")], id="explicit-before-after"),
        pytest.param("Latency 100 ms → 50 ms.", [("time", -0.5, "decrease")], id="unicode-arrow"),
        pytest.param("Latency 100 ms => 50 ms.", [("time", -0.5, "decrease")], id="equals-arrow"),
        pytest.param("Drops from 100 ms to 50 ms.", [("time", -0.5, "decrease")], id="unit-governed-transition"),
        pytest.param("Latency: before 120 ms, after 80 ms.", [("time", -0.3333, "decrease")], id="labeled-quantity"),
        pytest.param("Before 120 ms after 80 ms.", [("time", -0.3333, "decrease")], id="before-after-without-comma"),
        pytest.param("Poll every 5 s, after 10 s of startup.", [], id="unpaired-after-label"),
        pytest.param("Cuts latency by 2x.", [("time", -0.5, "decrease")], id="factor-bound-to-measured-object"),
        pytest.param("Reduces peak memory from 4 GB to 3 GB.", [("memory", -0.25, "decrease")], id="memory-verb-pair"),
        pytest.param("Training now takes 80 s instead of 120 s.", [("time", -0.3333, "decrease")], id="measured-duration-instead"),
        pytest.param("Startup takes 2 s instead of 10 s.", [("time", None, None)], id="startup-duration-instead"),
        pytest.param("Takes 2 s instead of 10 s.", [("time", -0.8, "decrease")], id="duration-instead"),
        pytest.param("Inference now takes 40 ms (was 120 ms).", [("time", None, None)], id="time-was-parenthetical"),
        pytest.param("Peak RSS: 2.1 GB (was 3.4 GB).", [("memory", None, None)], id="memory-was-parenthetical"),
        pytest.param("Saves 2 GB of RAM.", [("memory", None, "decrease")], id="absolute-memory-savings"),
        pytest.param("Saves 30 s per epoch.", [("time", None, "decrease")], id="absolute-time-savings"),
        pytest.param("Shaves 30% off the runtime.", [("time", None, None)], id="percent-off-measured-quantity"),
        pytest.param("Brings latency down to 80 ms from 120 ms.", [("time", None, None)], id="after-before-order"),
        pytest.param("Previously latency was 120 ms; this PR brings it to 80 ms.", [("time", None, None)], id="comparison-across-semicolon"),
        pytest.param("It used to take 10 s; now it takes 2 s.", [("time", None, None)], id="used-to-and-now"),
        pytest.param("Previously 10 s, now 2 s.", [("time", None, None)], id="terse-history-comparison"),
        pytest.param("Loading is twice as fast.", [("time", None, None)], id="written-speed-factor"),
        pytest.param("This PR reduces the memory required to buffer requests.", [("memory", None, None)], id="relative-object-clause"),
        pytest.param("Training takes 2 s vs. 8 s.", [("time", None, None)], id="versus-in-duration-comparison"),
        pytest.param("Measured 2 GB rather than 4 GB.", [("memory", None, None)], id="than-in-unit-comparison"),
        pytest.param("Processing takes 2 s instead of 4 GB.", [], id="different-unit-families"),
        pytest.param("Memory: Python 3.10 -> 3.11.", [], id="unrelated-pair-after-metric-heading"),
        pytest.param("Memory: resize the test fixture from 2 s to 10 GB.", [], id="metric-heading-over-incompatible-pair"),
        pytest.param("Numpy is now 2x faster than pure Python.", [("time", None, None)], id="named-library-subject"),
        pytest.param("CPython is now 30% faster.", [("time", None, None)], id="named-runtime-subject"),
        pytest.param("Upstream now runs 2x faster.", [("time", None, None)], id="upstream-subject"),
        pytest.param("Main is now slower than this branch.", [("time", None, None)], id="main-subject"),
        pytest.param("The baseline now uses 20% less memory than this PR.", [("memory", None, None)], id="baseline-subject"),
        pytest.param("Latency dropped from 120 ms to 80 ms in v1.4.", [("time", None, None)], id="version-after-transition"),
        pytest.param("Latency dropped from 100 ms to 60 ms in the previous release.", [("time", None, None)], id="release-after-transition"),
        pytest.param("Throughput fell from 100 it/s to 50 it/s on main.", [("throughput", None, None)], id="branch-after-transition"),
        pytest.param("Memory grew from 1 GB to 4 GB last release.", [("memory", None, None)], id="release-after-memory-effect"),
        pytest.param("Latency drops by 30% after 2.0.", [("time", None, None)], id="version-after-percentage"),
        pytest.param("This PR makes no claim of being 2x faster.", [("time", None, None)], id="negated-makes"),
        pytest.param("This patch makes inference 2x faster.", [("time", -0.5, "decrease")], id="affirmative-makes"),
        pytest.param("This PR makes the README 2x faster.", [("time", None, None)], id="unmodeled-comparative-object"),
        pytest.param("This branch cuts latency by 30%.", [("time", -0.3, "decrease")], id="this-branch-subject"),
        pytest.param("The baseline runs faster but uses more memory.", [("time", None, None), ("memory", None, None)], id="baseline-subject-inherited"),
        pytest.param("Main is faster while using less memory.", [("time", None, None), ("memory", None, None)], id="while-inherits-subject"),
        pytest.param("Main is slower; uses 20% less memory.", [("time", None, None), ("memory", None, None)], id="semicolon-inherits-subject"),
        pytest.param("This branch's baseline is 2x faster, but uses less memory.",
                     [("time", None, None), ("memory", None, None)], id="possessive-is-not-head-subject"),
        pytest.param("It is unclear whether Main is faster and cuts latency by 30%.",
                     [("time", None, None), ("time", None, None)], id="embedded-predicate"),
        pytest.param("This PR adds a test and Main runs faster and uses less memory.",
                     [("time", None, None), ("memory", None, None)], id="subject-changes-in-coordination"),
        pytest.param("This PR runs faster but uses more memory.", [("time", None, "decrease"), ("memory", None, "increase")], id="head-subject-inherited"),
        pytest.param("Gives more time to slow CI runners.", [], id="time-allowance-to-ci"),
        pytest.param("Gives more time to users before the session expires.", [], id="time-allowance-to-users"),
        pytest.param("Has more time to finish.", [], id="time-allowance-to-finish"),
        pytest.param("Allows more time for reviewers.", [], id="time-allowance-for-recipient"),
        pytest.param("Takes more time on the first call.", [("time", None, None)], id="measured-time-comparative"),
        pytest.param("Uses `time.monotonic()` instead of `time.time()`.", [], id="inline-module-call"),
        pytest.param("Use `time.perf_counter` rather than `time.time`.", [], id="inline-module-attribute"),
    ],
)  # fmt: skip
def test_claim_prose_corpus_retains_candidates_and_binds_measured_effects(text: str, expected: list[tuple]) -> None:
    """Keep ambiguous performance text visible and quantify only stated head effects."""
    claims = rcr.extract_claims([("body", text)])["claims"]
    assert [(c["metric"], c["claimed_relative"], c["expected_direction"]) for c in claims] == expected
    for claim in claims:
        if claim["expected_direction"] is None:
            assert (claim["parse_status"], claim["verdict"], claim["reason"]) == (
                "unparsed",
                "not-run",
                "claim not parsed",
            )
    if "Windows sees" in text:
        assert [c["requires_os"] for c in claims] == [["linux"], ["windows"]]


@pytest.mark.parametrize(
    ("text", "basis", "requirement"),
    [
        pytest.param("On Windows, loading is 2x faster.", "os-absent", "windows", id="line-os"),
        pytest.param("On x86_64, loading is 2x faster.", "arch-absent", "x86_64", id="line-arch"),
        pytest.param("On x64, loading is 2x faster.", "arch-absent", "x86_64", id="architecture-alias"),
        pytest.param("Windows: import is 2x faster.", "os-absent", "windows", id="os-heading"),
        pytest.param("Windows 2x faster, Linux unchanged.", "os-absent", "windows", id="contrasted-os"),
        pytest.param("| OS | Before (ms) | After (ms) |\n|---|---|---|\n| Windows | 120 | 80 |",
                     "os-absent", "windows", id="os-table-label"),
    ],
)  # fmt: skip
def test_claim_environment_binds_before_the_document_accelerator(text, basis, requirement) -> None:
    """A missing document-only CUDA accelerator cannot make an incompatible claim comparable."""
    host = {"os": "Linux", "native_arch": "arm64", "accelerators": []}
    claim = _claim(f"Benchmarked on an RTX 4090 box.\n{text}")
    decision = rcr.hardware_decision(claim, host)
    assert (decision["decision"], decision["basis"], decision["requires"]) == ("not-comparable", basis, requirement)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Load time is lower: 1 GB -> 800 ms.", id="arrow-across-dimensions"),
        pytest.param("Peak memory: 100% less memory.", id="whole-quantity-removed"),
        pytest.param("Gives 100% lower throughput.", id="rate-to-zero"),
    ],
)
def test_stated_quantities_without_a_coherent_ratio_carry_no_magnitude(text: str) -> None:
    """A pair in different dimensions, or a decrease of the whole quantity, keeps its direction and no magnitude.

    A wrong magnitude would turn a correctly measured effect into ``under half the claim``; no magnitude only asks for
    the claimed direction beyond noise.
    """
    claim = _claim(text)
    assert claim["claimed_relative"] is None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Reduces the number of test files by 30%.", id="count-not-performance"),
        pytest.param("Increases test coverage by 20%.", id="coverage-not-performance"),
    ],
)
def test_verb_by_percent_outside_a_performance_context_is_no_claim(text: str) -> None:
    """The verb form ("reduces X by N%") counts only on a line about speed, time, memory or rate."""
    assert rcr.extract_claims([("body", text)])["claims"] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Compared to this PR, latency is 20% lower.",
                     [("time", None, None)], id="opening-comparator-names-head-as-baseline"),
        pytest.param("Latency is 20% lower relative to this patch.",
                     [("time", None, None)], id="trailing-comparator-names-head-as-baseline"),
        pytest.param("Latency is 20% lower vs this change.",
                     [("time", None, None)], id="versus-names-head-as-baseline"),
        pytest.param("Before: 10 s to 30 s.", [("time", None, None)], id="before-label-only"),
        pytest.param("Latency before: 10 s to 30 s.", [("time", None, None)], id="metric-before-label-only"),
        pytest.param("100 requests/s -> 200 tokens/s.", [("throughput", None, None)], id="unlike-rate-arrow"),
        pytest.param("Throughput rises from 100 requests/s to 200 tokens/s.",
                     [("throughput", None, None)], id="unlike-rate-transition"),
        pytest.param("Throughput 100 frames/s -> 200 fps.",
                     [("throughput", -0.5, "decrease")], id="equivalent-rate-units"),
        pytest.param("Trims latency by 20%.", [("time", None, None)], id="unknown-effect-verb"),
        pytest.param("25% reduction in peak RSS.", [("memory", -0.25, "decrease")], id="nominal-effect"),
        pytest.param("This PR gives clients 10 s instead of 5 s to reconnect.", [], id="instead-of-allowance"),
        pytest.param("Extends the time from 5 s to 10 s to reconnect.", [], id="transition-allowance"),
        pytest.param("Gives more time.", [], id="unqualified-allowance"),
    ],
)  # fmt: skip
def test_effect_evidence_requires_a_measured_head_comparison(text: str, expected: list[tuple]) -> None:
    """Retain stated effects and quantify only equivalent measurements attributed to the head."""
    claims = rcr.extract_claims([("body", text)])["claims"]
    assert [(c["metric"], c["claimed_relative"], c["expected_direction"]) for c in claims] == expected


@pytest.mark.parametrize("separator", [" and ", ", ", "; "])
@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        pytest.param("Cuts latency by 30%", "reduces memory by 20%",
                     [("decrease", -0.3), ("decrease", -0.2)], id="shared-head"),
        pytest.param("Main uses less memory", "reduces memory by 20%",
                     [(None, None), (None, None)], id="foreign-subject"),
        pytest.param("Main uses less memory", "latency falls by 50%",
                     [(None, None), (None, None)], id="foreign-measured-subject"),
    ],
)  # fmt: skip
def test_coordinated_effects_have_independent_source_spans(
    separator: str, first: str, second: str, expected: list[tuple]
) -> None:
    """Each measured predicate has a separate record and inherits only an established head subject."""
    claims = rcr.extract_claims([("body", f"{first}{separator}{second}")])["claims"]
    assert [c["claim_text"] for c in claims] == [first, second]
    assert [(c["expected_direction"], c["claimed_relative"]) for c in claims] == expected


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        pytest.param("Python allocations drop by 40%.", "allocation-count", id="allocation-count"),
        pytest.param("Python allocations drop from 2 GB to 1 GB.", "heap", id="allocation-bytes"),
        pytest.param("RSS goes from 1.2 GB to 800 MB.", "rss", id="rss"),
        pytest.param("Uses 30% less memory.", "rss", id="unqualified-memory-is-process-memory"),
        pytest.param("Reduces VRAM usage by 40%: 2 GB -> 1.2 GB.", "device", id="vram-is-device"),
        pytest.param("The decode is 2x faster.", None, id="time-claim-has-no-memory-kind"),
    ],
)
def test_memory_claims_record_which_memory_they_name(text: str, kind: str | None) -> None:
    """A memory claim records whether it names the Python heap, the process (RSS) or device memory.

    The verdict compares only the quantity the claim names; a heap measurement says nothing about RSS or VRAM.
    """
    assert _claim(text)["memory_kind"] == kind


def test_device_memory_claim_requires_a_gpu() -> None:
    """VRAM wording binds the claim to a GPU class, so a CPU-only host reports exactly that requirement."""
    claim = _claim("Reduces VRAM usage by 40%: 2 GB -> 1.2 GB.")
    decision = rcr.hardware_decision(claim, _HOST_NO_CUDA)
    assert (claim["requires"], decision["decision"], decision["basis"]) == (["gpu"], "not-comparable",
                                                                            "accelerator-absent")  # fmt: skip


_BEFORE_AFTER = (
    "| Hardware | Before (µs/call) | After (µs/call) | Delta |\n|---|---|---|---|\n"
    "| AMD Ryzen 7 7800X3D (CPU) | 339.8 | 322.5 | −17.4 (−5.1%) |\n"
)
_TRANSITIONS = (
    "| masks | size | IoU (ms) | IoU peak (MiB) |\n|---|---|---|---|\n| 50 | 480x640 | 39 to 9 | 117 to 7 |\n"
)
_COMPARISONS = _TRANSITIONS.replace("IoU (ms)", "IoU latency Before/After (ms)").replace(
    "IoU peak (MiB)", "IoU peak memory Before/After (MiB)"
)
_QUALITATIVE = (
    "| case | develop | this PR |\n|---|---|---|\n"
    "| keypoint preview | `KeyError: 'keypoints'` after 3 s | probes batch 3 with 6 accumulation steps, trains |\n"
)


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        pytest.param(_BEFORE_AFTER, [("time", -0.0509, "decrease", "x86_64")], id="before-after-columns"),
        pytest.param(_TRANSITIONS, [("time", None, None, None), ("memory", None, None, None)],
                     id="interval-cells-unit-in-header"),
        pytest.param(_COMPARISONS, [("time", -0.7692, "decrease", None), ("memory", -0.9402, "decrease", None)],
                     id="transition-cells-unit-in-header"),
        pytest.param(_QUALITATIVE, [], id="qualitative-cells-are-not-claims"),
        pytest.param("| step | before | after |\n|---|---|---|\n| load | 2 s | 500 ms |\n",
                     [("time", -0.75, "decrease", None)], id="cell-units-normalized"),
        pytest.param("| step | before | after |\n|---|---|---|\n| peak | 1.5 GB | 900 MB |\n",
                     [("memory", -0.4, "decrease", None)], id="memory-from-cell-units"),
        pytest.param("| step | time transition |\n|---|---|\n| load | 1 s to 400 ms |\n",
                     [("time", -0.6, "decrease", None)], id="transition-cell-units"),
        pytest.param("| case | before (ms) | after (ms) |\n|---|---|---|\n| big | 1,200 | 800 |\n",
                     [("time", -0.3333, "decrease", None)], id="grouped-digits"),
        pytest.param("| step | before | after |\n|---|---|---|\n| load | 2 s | 900 MB |\n", [],
                     id="cells-in-different-dimensions"),
        pytest.param("| step | before | after |\n|---|---|---|\n| load | 2 s | 500 |\n", [],
                     id="one-cell-without-a-unit"),
        pytest.param("| step | before | after |\n|---|---|---|\n| load | 100 requests/s | 200 tokens/s |\n",
                     [("throughput", None, None, None)], id="columns-with-unlike-rates"),
        pytest.param("| step | throughput |\n|---|---|\n| load | 100 requests/s -> 200 tokens/s |\n",
                     [("throughput", None, None, None)], id="transition-with-unlike-rates"),
        pytest.param("| step | before (frames/s) | after (fps) |\n|---|---|---|\n| load | 100 | 200 |\n",
                     [("throughput", -0.5, "decrease", None)], id="columns-with-equivalent-rates"),
    ],
)  # fmt: skip
def test_table_claims(table: str, expected: list[tuple[str, float, str, str | None]]) -> None:
    """Benchmark tables yield one claim per measured cell pair; prose in a cell never becomes a number.

    Real PR bodies put their figures in tables (before/after columns, or "39 to 9" cells under a unit header); a
    sentence-only extractor missed them. A row's own hardware (the Ryzen label) is the author's hardware for it.
    """
    claims = rcr.extract_claims([("body", table)])["claims"]
    got = [(c["metric"], c["claimed_relative"], c["expected_direction"], c["cpu_family"]) for c in claims]  # type: ignore[index]
    assert got == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("| Metric | Before | After |\n|---|---|---|\n| Throughput (img/s) | 100 | 200 |\n"
                     "| Peak RSS (MB) | 500 | 300 |\n| Allocations | 1000 | 500 |",
                     [("throughput", -0.5, "decrease", None), ("memory", -0.4, "decrease", "peak-rss"),
                      ("memory", -0.5, "decrease", "allocation-count")], id="row-quantity"),
        pytest.param("| Metric | main | this PR |\n|---|---|---|\n| QPS | 100 | 200 |",
                     [("throughput", -0.5, "decrease", None)], id="row-rate-with-revision-headers"),
        pytest.param("| Case | Allocations Before/After |\n|---|---|\n| Decoder | 1000 to 500 |",
                     [("memory", -0.5, "decrease", "allocation-count")], id="transition-column-quantity"),
        pytest.param("| Case | Latency range (ms) |\n|---|---|\n| Decoder | 10 to 20 |",
                     [("time", None, None, None)], id="interval-is-not-a-head-comparison"),
        pytest.param("| Before (ms/request) | After (ms/batch) |\n|---|---|\n| 100 | 80 |",
                     [("time", None, None, None)], id="headers-with-different-denominators"),
        pytest.param("| Before (ms/request) | After (s/request) |\n|---|---|\n| 100 | 0.08 |",
                     [("time", -0.2, "decrease", None)], id="headers-with-equivalent-composite-units"),
        pytest.param("Latency: before 100 ms/request, after 80 ms/batch.",
                     [("time", None, None, None)], id="prose-with-different-denominators"),
        pytest.param("| Before timeout (ms) | After timeout (ms) |\n|---|---|\n| 100 | 80 |",
                     [], id="configuration-columns"),
        pytest.param("| Case | Timeout Transition (ms) |\n|---|---|\n| load | 100 to 80 |",
                     [], id="configuration-transition"),
        pytest.param("| Case | Latency before (ms) | Latency after (ms) | Before RSS (MB) | After RSS (MB) |\n"
                     "|---|---|---|---|---|\n| Decoder | 100 | 80 | 400 | 300 |",
                     [("time", -0.2, "decrease", None), ("memory", -0.25, "decrease", "rss")],
                     id="independent-column-quantities"),
        pytest.param("Throughput: 100 MB/s -> 200 MB/s. Write speed: 100 MB/s -> 200 MB/s.",
                     [("throughput", -0.5, "decrease", None)] * 2, id="byte-rate-prose"),
        pytest.param("Latency on main is 20% lower.",
                     [("time", None, None, None)], id="other-revision-subject"),
        pytest.param("This PR uses 30% fewer allocations (2 MB less).",
                     [("memory", -0.3, "decrease", "allocation-count")], id="count-with-byte-context"),
    ],
)  # fmt: skip
def test_claim_quantity_and_comparison(text: str, expected: list[tuple]) -> None:
    """Keep each measured quantity and require evidence of a comparable head effect before granting direction."""
    claims = rcr.extract_claims([("body", text)])["claims"]
    assert [
        (item["metric"], item["claimed_relative"], item["expected_direction"], item["memory_kind"]) for item in claims
    ] == expected


@pytest.mark.parametrize("heading", ["Branch", "Fix", "PR", "New"])
def test_revision_like_row_label_does_not_hide_measurement_columns(heading: str) -> None:
    """Choose the actual numeric after column even if the row label resembles a revision header."""
    claim = _claim(f"| {heading} | Before (ms) | After (ms) |\n|---|---|---|\n| decoder | 100 | 80 |")
    assert (claim["metric"], claim["claimed_relative"]) == ("time", -0.2)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Adds 20% more test coverage for the parser.", id="coverage-not-performance"),
        pytest.param("Bumps the version from 1.2 to 1.3.", id="version-bump"),
        pytest.param("This PR adds a benchmark script.", id="no-direction"),
    ],
)
def test_text_without_a_performance_claim_is_ignored(text: str) -> None:
    """Numbers and comparative words outside a speed, time or memory context are not claims.

    A false claim would turn into a ``not-reproduced`` verdict the author never earned.
    """
    assert rcr.extract_claims([("body", text)])["claims"] == []


def test_comment_claims_carry_their_author(tmp_path: Path) -> None:
    """Claims in saved PR comments are extracted with the commenter as the source, and a missing file is skipped.

    A maintainer's measurement in a comment is a claim too; the label tells the report whose number it is.
    """
    meta, comments = tmp_path / "pr-meta.json", tmp_path / "pr-comments.json"
    meta.write_text(json.dumps({"body": "Refactors the loader."}), encoding="utf-8")
    comments.write_text(json.dumps({"comments": [{"author": {"login": "alice"}, "body": "Now 2x faster here."}]}),
                        encoding="utf-8")  # fmt: skip
    sources = rcr.read_pr_texts(meta, [], comments) + rcr.read_pr_texts(meta, [], tmp_path / "absent.json")
    assert [label for label, _ in sources] == ["body", "comment:alice", "body"]


def test_document_hardware_applies_when_the_line_names_none() -> None:
    """A hardware sentence elsewhere in the body becomes the author's hardware for an unqualified claim.

    The scope is recorded, so the report can say the hardware came from the document, not the claim line.
    """
    doc = rcr.extract_claims([("body", "Benchmarked on an RTX 4090.\nThe decoder is 2x faster.")])
    claim = doc["claims"][0]  # type: ignore[index]
    assert (claim["accelerator"], claim["author_hw"]["scope"]) == ("cuda", "document")


def test_apple_machine_name_is_a_cpu_family_not_a_gpu_claim() -> None:
    """ "M2 Pro" names the machine (an arm64 CPU), so the claim compares on any arm64 host without needing Metal.

    Only the device API words (``mps``, ``metal``) make a claim require the Apple GPU.
    """
    claim = _claim("On my M2 Pro laptop the loop is 3x faster.")
    assert (claim["accelerator"], claim["cpu_family"]) == (None, "arm64")


class TestHardwareDecision:
    """Only a missing accelerator class blocks a claim, and the reason names exactly that class and its evidence."""

    @pytest.mark.parametrize(
        ("need", "reason"),
        [
            pytest.param("cuda", "not reproducible on this host: requires CUDA; host has no CUDA device "
                         "(cuda: nvidia-smi not on PATH)", id="cuda"),
            pytest.param("tensorrt", "not reproducible on this host: requires CUDA (TensorRT); host has no CUDA device "
                         "(cuda: nvidia-smi not on PATH)", id="tensorrt"),
            pytest.param("gpu", "not reproducible on this host: requires CUDA or ROCm (an unnamed GPU); host has no "
                         "CUDA or ROCm device (cuda: nvidia-smi not on PATH; rocm: rocm-smi not on PATH)",
                         id="unnamed-gpu"),
        ],
    )  # fmt: skip
    def test_missing_accelerator_is_hardware_unavailable_with_its_evidence(self, need: str, reason: str) -> None:
        """The reason states the required class and the probe evidence of its absence, nothing broader.

        An Apple GPU on the host does not stand in for a CUDA claim, and no other gap is implied.
        """
        decision = rcr.hardware_decision({"accelerator": need}, _HOST_NO_CUDA)
        assert (decision["decision"], decision["comparable"], decision["reason"]) == ("not-comparable", False, reason)

    @pytest.mark.parametrize(
        ("family", "decision", "basis"),
        [
            pytest.param("arm64", "same-class", "same-cpu-family", id="same-family"),
            pytest.param("x86_64", "proportional", "similar-class-cpu", id="other-family"),
            pytest.param(None, "proportional", "author-hardware-unstated", id="unstated"),
        ],
    )
    def test_cpu_claims_always_compare_the_relative_effect(self, family: str | None, decision: str, basis: str) -> None:
        """Every CPU claim is comparable; the shared decision and the basis record whether the CPU family matches.

        A different CPU family changes absolute figures, not whether base and head can be compared on one host.
        """
        result = rcr.hardware_decision({"accelerator": None, "cpu_family": family}, _HOST_NO_CUDA)
        assert (result["comparable"], result["decision"], result["basis"]) == (True, decision, basis)


class TestHostClass:
    """The host class is a per-run fact; only a claim bound to another class is not reproducible here."""

    @pytest.mark.parametrize(
        ("line", "required"),
        [
            pytest.param("On Windows the path join is 3x faster.", (["windows"], []), id="windows-on-line"),
            pytest.param("Under WSL2 the loader is 2x faster.", (["linux"], []), id="wsl-is-linux"),
            pytest.param("On macOS the decode is 2x faster.", (["macos"], []), id="macos-on-line"),
            pytest.param("On x86_64 the SIMD path is 2x faster.", ([], ["x86_64"]), id="arch-on-line"),
            pytest.param("The decode is 2x faster.", ([], []), id="no-class"),
        ],
    )
    def test_os_and_arch_named_on_the_claim_line_bind_the_claim(self, line: str, required: tuple) -> None:
        """An OS or CPU architecture named on the claim line is the claim's class; WSL counts as Linux.

        Only the claim line binds: the author's own environment elsewhere in the body is recorded separately.
        """
        claim = _claim(line)
        assert (claim["requires_os"], claim["requires_arch"]) == required

    @pytest.mark.parametrize(
        ("body", "accelerator", "required_os"),
        [
            pytest.param("Benchmarked on a bare-metal Linux server.\nNMS is now 3x faster.", None, [],
                         id="bare-metal-is-not-apple-metal"),
            pytest.param("NMS over sliding windows is 2x faster.", None, [], id="sliding-windows-is-not-the-os"),
            pytest.param("On Windows 11 the path join is 3x faster.", None, ["windows"], id="windows-os-context"),
            pytest.param("The Metal backend is 2x faster on MPS.", "mps", [], id="apple-metal-context"),
        ],
    )  # fmt: skip
    def test_class_words_bind_only_in_their_hardware_or_os_sense(
        self, body: str, accelerator: str | None, required_os: list[str]
    ) -> None:
        """A word that also names a hardware or OS class binds the claim only where it means that class.

        "Bare-metal" is a server, not Apple's Metal API; "sliding windows" is an algorithm, not the operating system.
        """
        claim = rcr.extract_claims([("body", body)])["claims"][-1]  # type: ignore[index]
        assert (claim["accelerator"], claim["requires_os"]) == (accelerator, required_os)  # type: ignore[index]

    def test_document_accelerator_compares_with_a_caveat_instead_of_blocking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An accelerator named only elsewhere in the body is the author's environment, not the claim's requirement.

        "No GPU needed" or a benchmark-machine sentence in another paragraph must not make a CPU claim ``hardware-
        unavailable``; the claim is measured here and its verdict reason says which class the body named.
        """
        claim = rcr.extract_claims([("body", "NMS is now 3x faster.\n\nNo GPU needed.")])["claims"][0]  # type: ignore[index]
        decision = rcr.hardware_decision(claim, _HOST_NO_CUDA)  # type: ignore[arg-type]
        assert (claim["requires"], decision["comparable"], decision["basis"]) == (  # type: ignore[index]
            [], True, "document-accelerator-absent"
        )  # fmt: skip
        monkeypatch.setattr(rcr, "invoke", _FakeRuns({"base": 1.0, "head": 0.9}))
        measured = {**_time_claim(-0.6667), "hardware": decision}
        record = rcr.run_claims(_plan([measured]), _GUARD_MODE, 3, 30)["claims"][0]  # type: ignore[index]
        assert (record["verdict"], "GPU" in record["reason"]) == ("not-reproduced", True)  # type: ignore[index]

    def test_os_named_elsewhere_is_the_author_environment(self) -> None:
        """ "Tested on Ubuntu 22.04" in another sentence records the author's OS without binding the claim to it.

        A pure-Python speedup measured on Linux is still reproducible, as a relative effect, on another OS.
        """
        claim = rcr.extract_claims([("body", "Tested on Ubuntu 22.04.\nThe decode is 2x faster.")])["claims"][0]
        assert (claim["requires_os"], claim["author_os"]) == ([], "linux")  # type: ignore[index]

    @pytest.mark.parametrize(
        ("claim", "expected"),
        [
            pytest.param({"requires_os": ["windows"]},
                         (False, "os-absent", "not reproducible on this host: requires Windows; host is macOS"),
                         id="other-os"),
            pytest.param({"requires_arch": ["x86_64"]},
                         (False, "arch-absent", "not reproducible on this host: requires x86_64; host is arm64"),
                         id="other-arch"),
            pytest.param({"requires_os": ["macos"], "requires_arch": ["arm64"], "cpu_family": "arm64"},
                         (True, "same-cpu-family", "author and host CPU family arm64"), id="same-class"),
        ],
    )  # fmt: skip
    def test_class_mismatch_is_named_exactly_and_same_class_compares(self, claim: dict, expected: tuple) -> None:
        """A claim bound to another OS or architecture is not reproducible on this host, naming that class only.

        No emulation is attempted, and on a host of the claim's own class the reason says nothing about the host.
        """
        decision = rcr.hardware_decision({"accelerator": None, **claim}, _HOST_NO_CUDA)
        assert (decision["comparable"], decision["basis"], decision["reason"]) == expected

    def test_host_class_is_recorded_per_run_in_every_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The result and each record's host spec carry the host class as a fact, in the shared vocabulary.

        A different user's host is then visible in the report without being called a gap.
        """
        monkeypatch.setattr(rcr, "invoke", _FakeRuns({"base": 1.0, "head": 0.5}))
        plan = {**_plan([_time_claim()]), "host": {"os": "Linux", "machine": "x86_64", "accelerators": ["cuda"]}}
        result = rcr.run_claims(plan, _GUARD_MODE, 3, 30)
        expected = {"os": "linux", "machine": "x86_64", "native_arch": "x86_64", "accelerators": ["cuda"]}
        assert (result["host_class"], result["claims"][0]["host_spec"]["host_class"]) == (expected, expected)  # type: ignore[index]


class TestGateMode:
    """The run mode comes only from the review's recorded PR-code decision."""

    @pytest.fixture
    def files(self, tmp_path: Path) -> dict[str, Path]:
        """Write clause, snapshot and approval file paths under a temp run directory."""
        approvals = {side: tmp_path / f"claim-approval-{side}.txt" for side in ("head", "base")}
        for path in approvals.values():
            path.write_text("digest\n{}\n", encoding="utf-8")
        return {"clause": tmp_path / "code-clause.txt", "meta": tmp_path / "pr-meta.json", **approvals}

    @pytest.mark.parametrize(
        ("clause", "cross_repo", "expected"),
        [
            pytest.param("", True, "none", id="empty-clause"),
            pytest.param(_REFUSAL_CLAUSE, True, "none", id="refusal-clause"),
            pytest.param(rcr.SAME_REPO_CLAUSE, True, "none", id="direct-clause-on-fork"),
            pytest.param(rcr.SAME_REPO_CLAUSE, False, "direct", id="same-repo"),
            pytest.param(_APPROVAL_CLAUSE, True, "guard", id="approval-clause"),
        ],
    )
    def test_mode_follows_the_recorded_decision(
        self, files: dict[str, Path], clause: str, cross_repo: bool, expected: str
    ) -> None:
        """A direct run needs both the same-repo clause and a same-repo snapshot; anything unrecognized runs nothing.

        This mirrors the review's own reconcile rule: a direct-run clause beside a fork snapshot is forged or mistaken.
        A missing clause file is covered by the ``gate_mode`` doctest.
        """
        files["clause"].write_text(clause + "\n", encoding="utf-8")
        files["meta"].write_text(json.dumps({"isCrossRepository": cross_repo}), encoding="utf-8")
        mode = rcr.gate_mode(files["clause"], files["meta"], {"head": files["head"], "base": files["base"]})
        assert mode.kind == expected

    def test_empty_approval_file_means_no_approval(self, files: dict[str, Path]) -> None:
        """The authorize block empties each approval before binding, so a failed bind never leaves an older one usable.

        One side without a bound approval keeps the whole run from starting.
        """
        files["clause"].write_text(_APPROVAL_CLAUSE + "\n", encoding="utf-8")
        files["base"].write_text("", encoding="utf-8")
        mode = rcr.gate_mode(files["clause"], files["meta"], {"head": files["head"], "base": files["base"]})
        assert (mode.kind, "base" in mode.reason) == ("none", True)


def test_run_command_line_offers_no_way_around_the_gate() -> None:
    """``run`` accepts only the plan, the recorded clause, the snapshot, approval files and measurement settings.

    An option naming another guard, a direct mode or a skip would let a caller widen what the review decided.
    """
    parser = rcr._parser()
    run = parser._subparsers._group_actions[0].choices["run"]  # type: ignore[union-attr]
    options = {opt for action in run._actions for opt in action.option_strings}
    assert options == {"-h", "--help", "--plan", "--clause-file", "--pr-meta", "--head-approval", "--base-approval",
                       "--rounds", "--timeout", "--budget", "--out"}  # fmt: skip


def _plan(claims: list[dict[str, object]], status: str = "ready") -> dict[str, object]:
    """Build a ready plan with fixed trees and one shared argv for both sides."""
    return {"status": status, "reason": "" if status == "ready" else "no benchmark file supplied", "host": {"os": "X"},
            "claims": claims, "argv": ["envpy", "-I", "harness.py", "bench.py"], "env_python": "/env/bin/python",
            "env_dir": "/env", "readable_roots": ["/env"], "base": "b" * 40, "head": "h" * 40,
            "sides": {"base": {"tree": "/trees/base"}, "head": {"tree": "/trees/head"}}}  # fmt: skip


def _time_claim(relative: float | None = -0.5) -> dict[str, object]:
    """Build a comparable time claim expecting a decrease."""
    return {
        "id": "c1",
        "claim_text": "2x faster",
        "metric": "time",
        "claimed_relative": relative,
        "expected_direction": "decrease",
        "benchmarked": True,
        "hardware": {"comparable": True, "decision": "same-cpu-family"},
    }


def _cuda_claim() -> dict[str, object]:
    """Build a claim that needs CUDA on a host without it."""
    return {
        "id": "c2",
        "claim_text": "TensorRT 40% faster",
        "metric": "time",
        "claimed_relative": -0.29,
        "expected_direction": "decrease",
        "hardware": {"comparable": False, "decision": "accelerator-absent", "reason": "no CUDA (probe)"},
    }


class _FakeRuns:
    """Stand-in for the per-side process: answers each tree with a fixed time, or a fixed failure."""

    def __init__(
        self, times: dict[str, float], code: int = 0, extra: dict[str, dict[str, object]] | None = None
    ) -> None:
        self.times, self.code, self.extra = times, code, extra or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], env: dict[str, str], cwd: str, timeout: int) -> tuple[int, str, str]:
        """Record the command and print a harness result line for the tree it runs in."""
        self.calls.append(list(argv))
        side = "base" if cwd.endswith("base") else "head"
        if self.code:
            return self.code, "", "untrusted-guard: approval does not match this scan"
        line = json.dumps({rcr.RESULT_MARKER: 1, "time_s": self.times[side], **self.extra.get(side, {})})
        return 0, f"bench chatter\n{line}\n", "untrusted-guard: isolation=sandbox(test)\n"


_GUARD_MODE = rcr.RunMode("guard", "approval", {"head": "/r/a-head.txt", "base": "/r/a-base.txt"})


class TestRun:
    """Runs alternate base and head through the guard; any failure is ``not-run``, never ``not-reproduced``."""

    def test_guard_mode_runs_only_the_guard_run_command_in_abba_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every process is ``untrusted_code_guard.py run --approval-file``, alternating base and head per round.

        The helper never scans or binds; it only spends approvals the parent bound for each side.
        """
        fake = _FakeRuns({"base": 1.0, "head": 0.5})
        monkeypatch.setattr(rcr, "invoke", fake)
        rcr.run_claims(_plan([_time_claim()]), _GUARD_MODE, 2, 30)
        heads = [(c[2], c[3], c[4], c[5]) for c in fake.calls]
        assert set(heads) == {(str(rcr.GUARD), "run", "--approval-file", "/r/a-base.txt"),
                              (str(rcr.GUARD), "run", "--approval-file", "/r/a-head.txt")}  # fmt: skip
        assert [c[7].rsplit("/", 1)[-1] for c in fake.calls] == ["base", "head", "head", "base"]

    @pytest.mark.parametrize(
        ("head_time", "verdict"),
        [
            pytest.param(0.5, "reproduced", id="claimed-effect"),
            pytest.param(0.99, "not-reproduced", id="within-noise"),
            pytest.param(1.5, "not-reproduced", id="opposite-direction"),
            pytest.param(0.85, "not-reproduced", id="under-half-the-claim"),
        ],
    )
    def test_verdict_compares_the_relative_effect(
        self, monkeypatch: pytest.MonkeyPatch, head_time: float, verdict: str
    ) -> None:
        """The head must move the metric in the claimed direction, beyond noise, by at least half the claimed effect.

        Base runs at 1.0 s and the claim is 2x faster (-50%); the author's absolute numbers never enter the verdict.
        """
        monkeypatch.setattr(rcr, "invoke", _FakeRuns({"base": 1.0, "head": head_time}))
        record = rcr.run_claims(_plan([_time_claim()]), _GUARD_MODE, 3, 30)["claims"][0]  # type: ignore[index]
        assert record["verdict"] == verdict

    @pytest.mark.parametrize(
        ("code", "fragment"),
        [
            pytest.param(3, "contributor-code gate refused", id="gate-refusal"),
            pytest.param(1, "benchmark failed", id="benchmark-crash"),
        ],
    )
    def test_a_run_that_did_not_happen_is_not_run(
        self, monkeypatch: pytest.MonkeyPatch, code: int, fragment: str
    ) -> None:
        """A refusal or a crash is an execution gap with its reason, never evidence against the claim.

        The hardware-unavailable claim keeps its own verdict; nothing else is inferred from the failure.
        """
        monkeypatch.setattr(rcr, "invoke", _FakeRuns({"base": 1.0, "head": 0.5}, code=code))
        records = rcr.run_claims(_plan([_time_claim(), _cuda_claim()]), _GUARD_MODE, 3, 30)["claims"]
        assert [r["verdict"] for r in records] == ["not-run", "hardware-unavailable"]  # type: ignore[index]
        assert fragment in records[0]["reason"]  # type: ignore[index]

    def test_budget_stop_keeps_complete_rounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the run budget ends mid-run, complete rounds still give a verdict if there are at least three.

        The review block has a tool time cap; a run cut off by it would lose every measurement.
        """
        clock = iter([0.0] * 7)  # budget start and six runs (three rounds), then the budget is spent
        monkeypatch.setattr(rcr.time, "monotonic", lambda: next(clock, 1000.0))
        monkeypatch.setattr(rcr, "invoke", _FakeRuns({"base": 1.0, "head": 0.5}))
        result = rcr.run_claims(_plan([_time_claim()]), _GUARD_MODE, 5, 30, 100)
        record = result["claims"][0]  # type: ignore[index]
        assert (result["rounds_completed"], record["head_samples_n"], record["verdict"]) == (3, 3, "reproduced")

    def test_fewer_than_three_samples_give_no_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two rounds measure something, but no verdict is drawn from them: the claim is ``not-run`` with the reason.

        Three samples per side is the minimum both harnesses use before calling an effect reproduced or not.
        """
        monkeypatch.setattr(rcr, "invoke", _FakeRuns({"base": 1.0, "head": 0.5}))
        record = rcr.run_claims(_plan([_time_claim()]), _GUARD_MODE, 2, 30)["claims"][0]  # type: ignore[index]
        assert (record["verdict"], record["base_samples_n"]) == ("not-run", 2)

    def test_record_carries_the_shared_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every record carries the fields the Codex Rig claim record shares, with the same meanings.

        Reports from both harnesses then roll up into one ledger without a translation table.
        """
        monkeypatch.setattr(rcr, "invoke", _FakeRuns({"base": 1.0, "head": 0.5}))
        result = rcr.run_claims(_plan([_time_claim()]), _GUARD_MODE, 3, 30)
        record = result["claims"][0]  # type: ignore[index]
        shared = {"claim_text", "metric", "direction", "author_hw", "host_spec", "comparable_hardware", "method",
                  "base_samples_n", "head_samples_n", "base_median", "head_median", "relative_delta", "spread",
                  "verdict", "reason"}  # fmt: skip
        assert (shared - set(record), record["direction"], record["method"]["order"]) == (  # type: ignore[index]
            set(), "lower-is-better", "alternating"
        )  # fmt: skip

    @pytest.mark.parametrize(
        ("kind", "verdict", "fragment"),
        [
            pytest.param("heap", "reproduced", "", id="heap-on-tracemalloc"),
            pytest.param("rss", "not-reproduced", "", id="rss-on-process-growth"),
            pytest.param("device", "not-run", "device memory", id="device-never-measured-here"),
        ],
    )
    def test_memory_claim_is_judged_on_the_memory_it_names(
        self, monkeypatch: pytest.MonkeyPatch, kind: str, verdict: str, fragment: str
    ) -> None:
        """The Python heap halves while the process growth stays flat: each claim reads only its own quantity.

        A heap measurement would otherwise confirm (or refute) an RSS or VRAM claim it cannot see.
        """
        extra = {"base": {"tracemalloc_peak_mb": 10.0, "rss_growth_mb": 40.0},
                 "head": {"tracemalloc_peak_mb": 5.0, "rss_growth_mb": 40.0}}  # fmt: skip
        monkeypatch.setattr(rcr, "invoke", _FakeRuns({"base": 1.0, "head": 1.0}, extra=extra))
        claim = {**_time_claim(-0.5), "metric": "memory", "memory_kind": kind}
        record = rcr.run_claims(_plan([claim]), _GUARD_MODE, 3, 30)["claims"][0]  # type: ignore[index]
        assert (record["verdict"], fragment in record["reason"]) == (verdict, True)  # type: ignore[index]

    def test_unmeasurable_process_memory_is_not_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A host without a peak-RSS reading (native Windows) reports the RSS claim ``not-run``, never a verdict."""
        extra = {"base": {"rss_growth_mb": None}, "head": {"rss_growth_mb": None}}
        monkeypatch.setattr(rcr, "invoke", _FakeRuns({"base": 1.0, "head": 1.0}, extra=extra))
        claim = {**_time_claim(-0.5), "metric": "memory", "memory_kind": "rss"}
        record = rcr.run_claims(_plan([claim]), _GUARD_MODE, 3, 30)["claims"][0]  # type: ignore[index]
        assert (record["verdict"], "RSS" in record["reason"]) == ("not-run", True)  # type: ignore[index]

    def test_allocation_counts_do_not_use_peak_bytes(self) -> None:
        """A decrease in peak bytes supplies no evidence for a decrease in allocation count."""
        claim = _claim("This PR uses 30% fewer allocations.")
        claim.update(benchmarked=True, hardware={"comparable": True})
        stats = {"base": rcr.SideStats(memory=[10.0] * 3), "head": rcr.SideStats(memory=[5.0] * 3)}
        record = rcr.evaluate(_plan([claim]), _GUARD_MODE, stats, "", {})[0]
        assert (record["verdict"], record["base_samples_n"], record["relative_delta"]) == ("not-run", 0, None)
        assert "allocation count" in record["reason"]

    def test_peak_rss_verdict_uses_total_process_peak(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A lower total peak reproduces a peak RSS claim even when RSS growth moves in the opposite direction."""
        extra = {"base": {"peak_rss_mb": 400.0, "rss_growth_mb": 25.0},
                 "head": {"peak_rss_mb": 200.0, "rss_growth_mb": 50.0}}  # fmt: skip
        monkeypatch.setattr(rcr, "invoke", _FakeRuns({"base": 1.0, "head": 1.0}, extra=extra))
        claim = _claim("Peak RSS is 50% lower.")
        claim.update(benchmarked=True, hardware={"comparable": True})
        record = rcr.run_claims(_plan([claim]), _GUARD_MODE, 3, 30)["claims"][0]
        assert (record["verdict"], record["base_median"], record["head_median"]) == ("reproduced", 400.0, 200.0)

    def test_unselected_claim_is_no_benchmark(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A benchmark that measures one claim never judges another; the rest are ``no-benchmark`` with the target.

        A PR may claim speedups for several functions and sizes; one benchmark of one of them says nothing about the
        others.
        """
        monkeypatch.setattr(rcr, "invoke", _FakeRuns({"base": 1.0, "head": 0.5}))
        other = {**_time_claim(), "id": "c2", "benchmarked": False}
        records = rcr.run_claims(_plan([{**_time_claim(), "benchmarked": True}, other]), _GUARD_MODE, 3, 30)["claims"]
        assert [(r["verdict"], r["reason"]) for r in records][1] == (  # type: ignore[index]
            "no-benchmark", "the supplied benchmark measures c1 only"
        )  # fmt: skip

    @pytest.mark.parametrize(
        ("plan_status", "mode_kind", "verdict"),
        [
            pytest.param("no-benchmark", "guard", "no-benchmark", id="no-benchmark"),
            pytest.param("ready", "none", "not-run", id="no-approval"),
            pytest.param("not-run", "guard", "not-run", id="environment-unavailable"),
        ],
    )
    def test_nothing_runs_without_a_benchmark_or_an_approval(
        self, monkeypatch: pytest.MonkeyPatch, plan_status: str, mode_kind: str, verdict: str
    ) -> None:
        """Without a benchmark, an approval or an environment no process starts, and the verdict says which.

        These are the cases most likely to be mislabeled as a failed reproduction.
        """
        fake = _FakeRuns({"base": 1.0, "head": 0.5})
        monkeypatch.setattr(rcr, "invoke", fake)
        mode = _GUARD_MODE if mode_kind == "guard" else rcr.RunMode("none", "clause forbids running PR code")
        record = rcr.run_claims(_plan([_time_claim()], plan_status), mode, 3, 30)["claims"][0]  # type: ignore[index]
        assert (record["verdict"], fake.calls) == (verdict, [])


@pytest.mark.integration
def test_harness_reports_process_memory_growth_of_one_call(tmp_path: Path) -> None:
    """The shipped harness reports how far one call raises the process's resident peak above its post-setup peak.

    The interpreter, imports and ``setup()`` are excluded, so a call allocating 64 MiB reads as about 64 MiB of growth
    rather than a small fraction of the whole process. Native Windows has no peak-RSS reading and reports ``None``.
    """
    harness, bench = tmp_path / "harness.py", tmp_path / "bench.py"
    harness.write_text(rcr.HARNESS, encoding="utf-8")
    # Nonzero fill: a zeroed bytearray is lazily mapped on Linux, so its untouched pages never reach the resident peak.
    bench.write_text(
        "def bench():\n    block = bytearray(b'\\x01') * (64 * 1024 * 1024)\n    return len(block)\n", encoding="utf-8"
    )
    done = subprocess.run([sys.executable, "-I", str(harness), str(bench), "--reps", "1", "--warmup", "0", "--memory"],
                          capture_output=True, text=True, timeout=120, cwd=tmp_path, check=True)  # fmt: skip
    growth = rcr._result_line(done.stdout)["rss_growth_mb"]  # type: ignore[index]
    if sys.platform == "win32":
        assert growth is None
    else:
        assert 32 <= growth <= 256


@requires_git
class TestPlan:
    """``plan`` prepares the base tree outside the repository and refuses what it cannot run."""

    @pytest.fixture
    def pair(self, tmp_path: Path) -> dict[str, object]:
        """Build a two-commit repository, an environment document using this interpreter, and a benchmark file."""
        repo = init_repo(tmp_path / "repo")
        base = commit(
            repo, {"pyproject.toml": pyproject(name="synth"), "src/synth/__init__.py": "def f():\n    return 1\n"}
        )
        head = commit(repo, {"src/synth/__init__.py": "def f():\n    return 2\n"})
        bench = tmp_path / "bench.py"
        bench.write_text("from synth import f\n\n\ndef bench():\n    f()\n", encoding="utf-8")
        env = {
            "env_python": sys.executable,
            "env_dir": str(tmp_path / "env"),
            "python": {"base_prefix": sys.base_prefix},
            "host": {"native_arch": platform.machine(), "accelerators": [], "absent_accelerators": {}},
        }
        claims = {
            "claims": [
                {
                    "id": "c1",
                    "claim_text": "2x faster",
                    "metric": "time",
                    "claimed_relative": -0.5,
                    "expected_direction": "decrease",
                    "accelerator": None,
                    "cpu_family": None,
                }
            ]
        }
        return {"repo": repo, "base": base, "head": head, "bench": bench, "env": env, "claims": claims}  # fmt: skip

    def _inputs(
        self, pair: dict[str, object], work: Path, bench: Path | None, selected: tuple[str, ...] = ("c1",)
    ) -> rcr.PlanInputs:
        """Assemble plan inputs for the fixture pair."""
        return rcr.PlanInputs(pair["claims"], pair["env"], pair["repo"], str(pair["base"]), str(pair["head"]), work,
                              bench, 3, 1, selected)  # type: ignore[arg-type]  # fmt: skip

    def test_ready_plan_clones_the_base_outside_the_repository(self, pair: dict[str, object], tmp_path: Path) -> None:
        """The base tree is a shared clone at the base commit; the reviewed repository gains no worktree.

        Both sides get the same argv and the harness and benchmark are listed as bound files for the gate.
        """
        plan = rcr.build_plan(self._inputs(pair, tmp_path / "work", pair["bench"]))  # type: ignore[arg-type]
        base_tree = Path(plan["sides"]["base"]["tree"])  # type: ignore[index]
        assert (plan["status"], git(base_tree, "rev-parse", "HEAD")) == ("ready", pair["base"])
        assert plan["bind_files"] == [str(tmp_path / "work" / "harness.py"), str(tmp_path / "work" / "bench.py")]
        assert git(pair["repo"], "worktree", "list").count("\n") == 0  # type: ignore[arg-type]

    def test_guard_plan_uses_the_detached_head_after_review_checkout_changes(
        self, pair: dict[str, object], tmp_path: Path
    ) -> None:
        """The tree used for guard binding keeps the reviewed contents after the active checkout advances."""
        plan = rcr.build_plan(self._inputs(pair, tmp_path / "work", pair["bench"]))
        repo = Path(pair["repo"])
        head_tree = Path(plan["sides"]["head"]["tree"])
        commit(repo, {"src/synth/__init__.py": "def f():\n    return 3\n"})
        assert git(head_tree, "rev-parse", "HEAD") == pair["head"]
        assert (head_tree / "src" / "synth" / "__init__.py").read_text(encoding="utf-8") == "def f():\n    return 2\n"

    def test_uncommitted_change_in_the_head_checkout_is_not_run(self, pair: dict[str, object], tmp_path: Path) -> None:
        """A checkout at the reviewed commit but with local edits is never measured as the head.

        Direct and sandbox runs copy the working tree, so an uncommitted edit would be benchmarked as the PR.
        """
        (Path(pair["repo"]) / "src" / "synth" / "__init__.py").write_text("def f():\n    return 3\n", encoding="utf-8")  # type: ignore[arg-type]
        plan = rcr.build_plan(self._inputs(pair, tmp_path / "work", pair["bench"]))  # type: ignore[arg-type]
        assert (plan["status"], "uncommitted" in str(plan["reason"])) == ("not-run", True)

    def test_claim_selection_marks_what_the_benchmark_measures(self, pair: dict[str, object], tmp_path: Path) -> None:
        """The supplied claim IDs select the only claims covered by the benchmark."""
        claims = {"claims": [{**pair["claims"]["claims"][0], "id": "c1"}, {**pair["claims"]["claims"][0], "id": "c2"}]}  # type: ignore[index]
        inputs = rcr.PlanInputs(claims, pair["env"], pair["repo"], str(pair["base"]), str(pair["head"]),  # type: ignore[arg-type]
                                tmp_path / "work", pair["bench"], 3, 1, ("c2",))  # type: ignore[arg-type]  # fmt: skip
        plan = rcr.build_plan(inputs)
        assert [c["benchmarked"] for c in plan["claims"]] == [False, True]  # type: ignore[union-attr,index]

    def test_no_benchmark_prepares_nothing(self, pair: dict[str, object], tmp_path: Path) -> None:
        """Without a benchmark file the plan says so and clones nothing.

        The plan still lists the author's benchmark candidates so the reviewer can adapt one.
        """
        plan = rcr.build_plan(self._inputs(pair, tmp_path / "work", None))
        assert (plan["status"], (tmp_path / "work").exists()) == ("no-benchmark", False)

    def test_benchmark_without_claim_ids_covers_no_claims(self, pair: dict[str, object], tmp_path: Path) -> None:
        """A benchmark file alone cannot select a latency or memory claim or initiate a run."""
        pair["claims"] = rcr.extract_claims([("body", "Cuts latency by 30%.\nUses 20% less memory.")])
        work = tmp_path / "work"
        plan = rcr.build_plan(self._inputs(pair, work, pair["bench"], selected=()))  # type: ignore[arg-type]
        assert [c["benchmarked"] for c in plan["claims"]] == [False, False]  # type: ignore[union-attr,index]
        result = rcr.run_claims(plan, rcr.RunMode("direct", "same-repo"), 3, 30)
        assert [c["verdict"] for c in result["claims"]] == ["no-benchmark", "no-benchmark"]  # type: ignore[union-attr,index]
        assert (work.exists(), result["rounds_completed"]) == (False, 0)

    @pytest.mark.parametrize(
        "text",
        [
            "This PR uses 30% fewer allocations.",
            "| Case | Before p95 latency (ms) | After p95 latency (ms) |\n|---|---|---|\n| load | 100 | 80 |",
        ],
    )
    def test_selected_claim_without_matching_samples_does_not_start_a_benchmark(
        self, text: str, pair: dict[str, object], tmp_path: Path
    ) -> None:
        """An explicitly selected quantity the harness cannot measure receives no timing verdict."""
        pair["claims"] = rcr.extract_claims([("body", text)])
        work = tmp_path / "work"
        plan = rcr.build_plan(self._inputs(pair, work, pair["bench"]))
        records = rcr.evaluate(plan, _GUARD_MODE, None, "", {})
        assert (work.exists(), records[0]["verdict"]) == (False, "not-run")
        assert "is not measured by this harness" in records[0]["reason"]

    @pytest.mark.integration
    def test_unparsed_claim_reaches_results_without_running_a_benchmark(
        self, pair: dict[str, object], tmp_path: Path
    ) -> None:
        """An unparsed claim is reported even when a runnable benchmark was supplied."""
        sentence = "On Windows, runtime is now 2.5 ms instead of 10 ms."
        pair["claims"] = rcr.extract_claims([("body", sentence)])
        work = tmp_path / "work"
        plan = rcr.build_plan(self._inputs(pair, work, pair["bench"]))  # type: ignore[arg-type]
        result = rcr.run_claims(plan, rcr.RunMode("direct", "same-repo"), 3, 30)
        records = result["claims"]
        assert len(records) == 1
        record = records[0]
        assert (record["claim_text"], record["parse_status"], record["verdict"], record["reason"]) == (
            sentence, "unparsed", "not-run", "claim not parsed"
        )  # fmt: skip
        assert record["direction"] is record["expected_direction"] is record["relative_delta"] is None
        assert (plan["status"], work.exists(), result["rounds_completed"]) == ("no-benchmark", False, 0)

    def test_work_directory_inside_the_repository_is_refused(self, pair: dict[str, object]) -> None:
        """The bound files and base tree must live outside the reviewed tree, where the sandbox can read them.

        The guard hides the repository's private directories from the run, and the tree must stay unmodified.
        """
        with pytest.raises(rcr.ClaimReproError, match="inside the reviewed repository"):
            rcr.build_plan(self._inputs(pair, Path(pair["repo"]) / ".temp" / "work", pair["bench"]))  # type: ignore[arg-type]

    @pytest.fixture(params=["none", "head-commit", "head-working-tree", "base-working-tree"])
    def direct_plan(self, tmp_path: Path, request: pytest.FixtureRequest) -> dict[str, object]:
        """Prepare a speedup benchmark and exercise committed or uncommitted drift after planning."""
        repo = init_repo(tmp_path / "repo")
        slow = (
            "def work(n):\n    total = 0\n    for _ in range(n):\n        total += sum(range(200))\n    return total\n"
        )
        base = commit(repo, {"pyproject.toml": pyproject(name="synth"), "synth.py": slow})
        head = commit(repo, {"synth.py": "def work(n):\n    return sum(range(200)) * n\n"})
        bench = tmp_path / "bench.py"
        bench.write_text("from synth import work\n\n\ndef setup():\n    return 3000\n\n\ndef bench(n):\n    work(n)\n",
                         encoding="utf-8")  # fmt: skip
        env = {
            "env_python": sys.executable,
            "env_dir": "",
            "python": {"base_prefix": sys.base_prefix},
            "host": {"native_arch": platform.machine(), "accelerators": [], "absent_accelerators": {}},
        }
        claims = {"claims": [_claim("This PR makes work 3x faster.")]}  # fmt: skip
        plan = rcr.build_plan(rcr.PlanInputs(claims, env, repo, base, head, tmp_path / "work", bench, 5, 1, ("c1",)))
        if request.param == "head-commit":
            commit(repo, {"synth.py": "raise RuntimeError('drifted head')\n"})
        elif request.param == "head-working-tree":
            (repo / "synth.py").write_text("raise RuntimeError('dirty head')\n", encoding="utf-8")
        elif request.param == "base-working-tree":
            (tmp_path / "work" / "base" / "synth.py").write_text("raise RuntimeError('dirty base')\n", encoding="utf-8")
        return plan

    @pytest.mark.integration
    def test_direct_same_repo_run_reproduces_a_real_speedup(self, direct_plan: dict[str, object]) -> None:
        """A real direct run imports both planned revisions even when the source checkouts drift."""
        result = rcr.run_claims(direct_plan, rcr.RunMode("direct", "same-repo"), 3, 60)
        assert result["claims"][0]["verdict"] == "reproduced"  # type: ignore[index]
        assert (result["method"]["base"], result["method"]["head"], result["rounds_completed"]) == (  # type: ignore[index]
            direct_plan["base"], direct_plan["head"], 3
        )  # fmt: skip
