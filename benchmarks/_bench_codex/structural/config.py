"""Arm tables, permission-profile names, and frozen manifest coordinates."""

from __future__ import annotations

import itertools
from pathlib import Path

#: This package sits at ``benchmarks/_bench_codex/structural/``; sibling paths are anchored here
#: rather than off each module's own ``__file__``, which moves with the module.
PACKAGE_DIR = Path(__file__).resolve().parent
#: The ``benchmarks/`` directory, two levels above this package; base for suites, manifests and runner scripts.
BENCHMARKS_DIR = PACKAGE_DIR.parents[1]
#: The repository root, the parent of the ``benchmarks/`` directory.
REPO_ROOT = BENCHMARKS_DIR.parent
#: The entrypoint this package implements. Provenance records the pair, never either alone.
RUNNER_PATH = BENCHMARKS_DIR / "run-codex-structural.py"

#: Codex integration manifest that pins the locked task ordinals, suite and experiment revision.
PARITY_MANIFEST_PATH = BENCHMARKS_DIR / "manifests" / "codex-integration.json"
#: Canonical arm labels in declared order: plain, optional Codemap, required Codemap.
CODEX_STRUCTURAL_ARMS = ("A_plain", "B_auto", "C_strict")
#: Every ordering of the three arms; one is chosen per task and repetition to counterbalance arm order.
_COUNTERBALANCED_ARM_ORDERS = tuple(itertools.permutations(CODEX_STRUCTURAL_ARMS))
#: Public alias of the canonical Codex arm tuple.
ARMS = CODEX_STRUCTURAL_ARMS
#: Default Codex model for provider-parity runs.
PARITY_CODEX_MODEL = "gpt-6.1-sol"
#: The only reasoning-effort setting provider-parity runs accept; the CLI rejects any other value.
PARITY_CODEX_REASONING_EFFORT = "high"
#: Name of the Codex executable, resolved from PATH when invoked.
_CODEX_BIN = "codex"
#: Task dictionary key under which the adapter stores provenance; removed to recover the canonical task.
_PROVENANCE_KEY = "_codex_provenance"
#: Identifier of the native-item telemetry contract recorded with each run for later compatibility checks.
_NATIVE_ITEM_TELEMETRY_CONTRACT_ID = "installed-skill-binding-locked-query-components-v3"
#: Codex permission profile applied to the plain arm, which has no Codemap access.
_PLAIN_PERMISSION_PROFILE = "provider-parity-plain"
#: Codex permission profile applied to the arms that may use Codemap.
_CODEMAP_PERMISSION_PROFILE = "provider-parity-codemap"
#: Name of the frozen plugin marketplace installed into each disposable Codex home; manifests must carry it.
_FROZEN_MARKETPLACE_NAME = "borda-ai-rig-frozen"
#: Largest auth file, in bytes (1 MiB), that provisioning will read.
_AUTH_MAX_BYTES = 1024 * 1024
#: Environment variable holding a JSON array of absolute evaluator directories that measured cells must not read.
_BENCHMARK_EVIDENCE_ROOTS_ENV = "BENCHMARK_EVIDENCE_ROOTS"
