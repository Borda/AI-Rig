"""Arm tables, permission-profile names, and frozen manifest coordinates."""

from __future__ import annotations

import itertools
from pathlib import Path


#: This package sits at ``benchmarks/_bench_codex/structural/``; sibling paths are anchored here
#: rather than off each module's own ``__file__``, which moves with the module.
PACKAGE_DIR = Path(__file__).resolve().parent
BENCHMARKS_DIR = PACKAGE_DIR.parents[1]
REPO_ROOT = BENCHMARKS_DIR.parent
#: The entrypoint this package implements. Provenance records the pair, never either alone.
RUNNER_PATH = BENCHMARKS_DIR / "run-codex-structural.py"

PARITY_MANIFEST_PATH = BENCHMARKS_DIR / "manifests" / "codex-integration.json"
CODEX_STRUCTURAL_ARMS = ("A_plain", "B_auto", "C_strict")
_COUNTERBALANCED_ARM_ORDERS = tuple(itertools.permutations(CODEX_STRUCTURAL_ARMS))
ARMS = CODEX_STRUCTURAL_ARMS
PARITY_CODEX_MODEL = "gpt-6.1-sol"
PARITY_CODEX_REASONING_EFFORT = "high"
_CODEX_BIN = "codex"
_PROVENANCE_KEY = "_codex_provenance"
_NATIVE_ITEM_TELEMETRY_CONTRACT_ID = "installed-skill-binding-locked-query-components-v3"
_PLAIN_PERMISSION_PROFILE = "provider-parity-plain"
_CODEMAP_PERMISSION_PROFILE = "provider-parity-codemap"
_FROZEN_MARKETPLACE_NAME = "borda-ai-rig-frozen"
_AUTH_MAX_BYTES = 1024 * 1024
_BENCHMARK_EVIDENCE_ROOTS_ENV = "BENCHMARK_EVIDENCE_ROOTS"
