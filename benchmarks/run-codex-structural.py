#!/usr/bin/env python3
"""Run the task-driven Codex provider-parity benchmark.

Omitting ``--tasks`` executes the complete locked catalog: 55 structural
questions, 6 ReadCrop tasks, 4 Fix-Single tasks, 3 Fix-Multi tasks, and 5
historical Patch tasks, for 73 tasks and 219 A/B/C cells. Family selectors such
as ``RC,FS,FM,PT`` and mixed exact IDs route to native stage scorers. Codex agentic exploration remains in
``run-codex-agentic.py``.

The 55 structural tasks are the former `bench`/real-codebase category, not a third benchmark type beside the
task-driven structural and agentic runners.

## What this measures

The same locked structural task, target repository, prompt, evaluator, and
600-second retry-inclusive per-coordinate wall-clock budget are used for three within-Codex arms. The
experiment asks whether Codemap availability reduces model input and elapsed
time without lowering task quality:

  A_plain    — Codemap absent; the locked index is inaccessible
  B_auto     — Codemap's compact CLI query is available; using it is the model's choice
  C_strict  — Codemap's installed query skill is required

The task series are identical to ``run-claude-structural.py``:

  SE — symbol extraction          FN — function call graph
  RV — review assistance          CQ — code quality
  BR — development blast radius   DG — debug from trace
  FT — feature scaffolding        RI — real issue
  DI — diff impact                GR — graph reasoning
  MB — module blast radius

This module owns only Codex-native process handling, isolated homes, permission
profiles, JSONL event normalization, and per-cell persistence. Task identity,
arm contracts, and scoring remain in ``_bench_common/provider_parity_contracts.py`` and the
shared Claude structural evaluator registry.

## Arms

Every arm receives the canonical task prompt unchanged plus a separately
fingerprinted arm envelope:

  A_plain
    Uses ``provider-parity-plain``. It has no Codemap plugin or writable path,
    and cannot read the locked index or copied authentication file.

  B_auto
    Uses ``provider-parity-codemap``. The direct ``$CODEMAP_BIN`` launcher and
    locked index are available and the model decides whether to query, so a
    cell that never queries has still followed this arm's contract.

  C_strict
    Uses the same treatment profile as B with the installed Codemap skill. It
    must use ``$codemap-py:query-code`` and complete one compact query;
    compliance and correctness are recorded separately.

Both treatment profiles extend ``:read-only``, disable network, and inherit no
shell environment. B/C may write only the index-local ``.index-rw`` coordination
directory. The model command cannot read the disposable home's ``auth.json``.

## Metrics

Each task × repetition × arm cell records:

  Headline inputs:
    provider, repetition, elapsed_s, input_tokens, cached_input_tokens, output_tokens,
    fresh_input_tokens, reasoning_output_tokens, quality_score, and correct

  Diagnostics:
    command_calls, Codemap calls/successes/errors, fallback calls, required-arm
    Codemap-use compliance, exact locked-query conformance, endpoint/target/option
    fitness, treatment adherence, extraction failure, contamination, retry count,
    execution index, native item counts, raw Codex events, and provider error
    classification

The runner writes raw cells; it does not declare an advantage. The manifest
analysis compares paired log input-token ratios and quality deltas, then applies
failure, adoption, and compliance guardrails.

Structural, ReadCrop, Fix-Single, Fix-Multi, and Patch keep separate telemetry,
metadata, input snapshots, scorers, and checksum ledgers. The aggregate root
records lifecycle and scope only; unlike quality metrics are never pooled.

## What is NOT measured

  - Agentic exploration from ``tasks-agentic.json``
  - Index construction cost; the locked index is prepared before model timing
  - A general Codemap advantage from one smoke task
  - Cross-provider raw token equality or pooled Claude/Codex results
  - Pooling stage-specific answer and executable quality metrics

## Quick start

Run the intended command with ``--dry-run`` first. The no-model preflight
validates target, index, permission, direct-launcher, installed-Skill, and
isolation contracts, prints the deterministic plan, then emits one aggregate
``SCOPE`` and exact ``PAID_COMMAND``. Omit ``--tasks`` for all 73 tasks; use
``--tasks RC,FS,FM,PT`` for families or mixed exact IDs for a targeted run.

The emitted execution command has no Boolean paid flag. Absence of
``--dry-run`` means model execution and requires a private ``auth.json``, a
fresh ``--run-dir``, and the aggregate ``--paid-approval`` token.

Primary options:

  --repo-path            locked target repository
  --manifest-path        active immutable benchmark manifest
  --index-path           frozen Codemap index
  --marketplace-root     local plugin marketplace used by the Skill arm
  --codemap-bin          absolute direct launcher used by the direct arm
  --model                locked Codex model identifier
  --tasks                optional family, exact-ID, or mixed selector
  --dry-run              no-model admission, plan, scope, and paid command
  --auth-source          private auth source for model execution
  --run-dir              fresh aggregate artifact directory
  --paid-approval        16-character aggregate token emitted by the dry run

## Requirements

  - Python 3.10+ and the benchmark dependency group
  - an installed Codex CLI that satisfies the exercised command and permission probes;
    its observed version is provenance, not an admission requirement
  - A clean target at PyTorch Lightning tag ``2.6.5`` and its locked index
  - A direct Codemap launcher for B and the local plugin marketplace root for C
  - For authenticated execution, a user-owned regular ``auth.json`` with mode
    0600; symlinks and group/other-readable files are rejected

## Failure conditions

The run fails closed before a model call when the manifest, target, task,
prompt, index, plugin, provided authentication, or permission-profile contract
differs from the active manifest. It also rejects dirty targets, symlinked or hard-linked
protected paths, credential/index exposure to A, missing index access for B/C,
or a broad coordination write surface.

During execution, timeouts, non-zero Codex exits, malformed/incomplete native
events, extraction failures, and target/index/coordination mutations remain
visible in the result. Only zero-token retryable transport failures may retry,
at most twice, within the original cell's timeout. A required arm without a successful compact query is recorded as
``compliance=false`` rather than rewritten as an incorrect task answer.

## Output

``--dry-run`` invokes no model and prints deterministic probe and plan rows.
Execution creates the aggregate directory exclusively, persists normalized
cells inside native stage children, and prints compact fixed-order progress.
Token counts show gross/cached/fresh input in telemetry and gross input in the
terminal. Interactive A/B/C rows are colored; redirected logs remain plain.
Each completed cell survives a later failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

# benchmarks/ is not a package; make its private shared packages importable
# regardless of how this script is launched (direct path, symlink, or any cwd).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import os  # noqa: F401
import subprocess  # noqa: F401
import tempfile  # noqa: F401
import time  # noqa: F401

from _bench_codex import runtime  # noqa: F401
from _bench_codex.structural.arms import (  # noqa: F401
    _arm_envelope,
    _manifest_arm_order,
    _print_result_block,
    arm_envelope,
)
from _bench_codex.structural.cli import _run_unified_execution, cli, load_index_relocation, main  # noqa: F401
from _bench_codex.structural.config import (  # noqa: F401
    _PROVENANCE_KEY,
    CODEX_STRUCTURAL_ARMS,
    PARITY_CODEX_MODEL,
    PARITY_CODEX_REASONING_EFFORT,
    PARITY_MANIFEST_PATH,
)
from _bench_codex.structural.diff_impact import (  # noqa: F401
    _capture_diff_impact_stage,
    _git_porcelain_status,
    _validate_codex_stratum,
    _validate_locked_runtime,
    build_codex_command,
)
from _bench_codex.structural.manifest import (  # noqa: F401
    _resolve_structural_task_selection,
    _validate_targeted_scope_request,
    _validate_unscoped_paid_task_ids,
    resolve_task_selection,
)
from _bench_codex.structural.models import CodexRun  # noqa: F401
from _bench_codex.structural.provenance import _repo_sha  # noqa: F401

# Reached by the shared _bench_codex stage modules and by run-codex-agentic.py, which both
# sibling-load this file by path rather than importing the package.
from _bench_codex.structural.provisioning import (  # noqa: F401
    ArmHome,
    TreatmentArtifactLockError,
    _admit_installed_skill_pair,
    _admit_staged_direct_cli,
    _aggregate_file_hashes,
    _archive_snapshot_file,
    _archive_snapshot_tree,
    _assert_safe_path_components,
    _benchmark_evidence_roots,
    _cleanup_coordination_root,
    _enabled_plugin_names,
    _install_codemap_plugin,
    _prepare_coordination_root,
    _registered_plugin_tables,
    _runtime_file_hashes,
    _shell_environment,
    _treatment_artifact_lock_mismatch_message,
    _treatment_artifact_version_mismatch_message,
    _untrusted_host_agent_roots,
    _validate_coordination_root,
    _validate_invocation_launcher,
    _verify_installed_plugin_pair,
    _verify_locked_codemap_python,
    _verify_permission_profile,
    _verify_plain_plugin_absent,
    _verify_treatment_artifact_locks,
    _write_frozen_marketplace,
    _write_input_snapshot,
    _write_permission_config,
    bind_executable_agent_workspace,
    prepare_arm_home,
    prepare_coordination_root,
    probe_arm_home,
)
from _bench_codex.structural.rescore import _initial_run_metadata, rescore_results  # noqa: F401
from _bench_codex.structural.runner import (  # noqa: F401
    NEW_PROCESS_GROUP,
    CodexRunner,
    _append_run,
    _assert_coordination_root_idle,
    _canonical_telemetry_path,
    _close_runner,
    _utc_now,
    _write_canonical_telemetry,
    _write_run_metadata,
)
from _bench_codex.structural.scoring import (  # noqa: F401
    _arm_compliance,
    _default_evaluator,
    _diff_impact_stager,
    _evaluator_identity,
    _locked_query_conformance,
    _locked_query_fitness,
    _pooling_ineligibility_reasons,
)
from _bench_codex.structural.tasks import load_tasks_with_provenance  # noqa: F401
from _bench_common.mutation_isolation import (
    ExecutableAgentWorkspace,
    create_executable_agent_workspace,
    relocate_frozen_index_for_worktree,
)

__all__ = (
    "ExecutableAgentWorkspace",
    "create_executable_agent_workspace",
    "relocate_frozen_index_for_worktree",
)


if __name__ == "__main__":
    from fire import Fire

    # Fire invokes the callable before reporting surplus flags. Fail first so a
    # removed legacy option can never widen or start an execution accidentally.
    removed_options = {
        "--arm",
        "--metadata-path",
        "--output-path",
        "--paid",
        "--repetitions",
        "--scope-sha256",
        "--study",
        "--task-id",
        "--tasks-path",
    }
    supplied_removed = sorted({argument.partition("=")[0] for argument in sys.argv[1:]} & removed_options)
    if supplied_removed:
        print(
            f"ERROR: removed option(s): {', '.join(supplied_removed)}. Use only --tasks for family or exact-task "
            "selection; omit --tasks for all supported tasks.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        Fire(cli)
    except (TreatmentArtifactLockError, FileExistsError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from None
