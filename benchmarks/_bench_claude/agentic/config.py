"""Arm tables and frozen coordinates shared by every agentic study."""

from pathlib import Path


from _bench_common import presentation  # noqa: E402

# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.python_source import resolve_relative_base  # noqa: E402,F401
from _bench_common.agentic_contracts import (  # noqa: E402
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.
from _bench_common.claude_stages import (  # noqa: E402,F401
    FIX_MULTI_TASKS_PATH,
    FIX_SINGLE_ARMS,
    FIX_SINGLE_TASKS_PATH,
    FixMultiContract,
    FixSingleContract,
    PARITY_MANIFEST_PATH,
    PATCH_TASKS_PATH,
    PurePosixPath,
    READCROP_ARMS,
    READCROP_TASKS_PATH,
    ReadcropUsage,
    StageIdentity,
    _FIX_MULTI_QUERY_ARGUMENTS,
    _FIX_SINGLE_QUERY_ARGUMENTS,
    _PATCH_QUERY_ARGUMENTS,
    _READCROP_ANSWER_RE,
    _absolute_codemap_launchers,
    _claude_codemap_evidence,
    _claude_event_summary,
    _claude_message_blocks,
    _command_arguments,
    _compact_query_result_succeeded,
    _frozen_index_recovery_attempted,
    _is_compact_query,
    _is_inside_workspace,
    _load_claude_fix_tasks,
    _manifest_sha256,
    _native_tool_result_succeeded,
    _outside_workspace_path_evidence,
    _patch_index_path,
    _patch_stage_identity,
    _provider_binding,
    _query_arguments_from_bash,
    _query_command_tail,
    _readcrop_module_path,
    _resolve_claude_fix_scope,
    _study_query_arguments,
    _tool_input_strings,
    _tool_result_text,
    _workspace_containment_roots,
    build_edit_task_contract,
    build_fix_multi_contract,
    build_fix_single_contract,
    build_readcrop_contract,
    extract_readcrop_symbol_source,
    load_claude_fix_multi_tasks,
    load_claude_fix_single_tasks,
    load_claude_patch_tasks,
    load_claude_readcrop_tasks,
    parse_claude_readcrop_events,
    parse_readcrop_answer,
    prompt_hash,
    readcrop_prompt,
    resolve_claude_fix_multi_scope,
    resolve_claude_fix_single_scope,
    resolve_claude_patch_scope,
    resolve_readcrop_scope,
    score_readcrop_answer,
    stage_contract_sha256,
)


_console = presentation.benchmark_console()

#: This package sits at ``benchmarks/_bench_claude/agentic/``; every sibling path is anchored
#: here rather than off each module's own ``__file__``, which moves with the module.
PACKAGE_DIR = Path(__file__).resolve().parent
BENCHMARKS_DIR = PACKAGE_DIR.parents[1]
REPO_ROOT = BENCHMARKS_DIR.parent
#: The entrypoint this package implements. Provenance records the pair, not either alone.
RUNNER_PATH = BENCHMARKS_DIR / "run-claude-agentic.py"

PATCH_INDEX_LOCKS_PATH = BENCHMARKS_DIR / "suites" / "patch-index-locks.json"
FIX_MULTI_ARMS = READCROP_ARMS
PATCH_ARMS = READCROP_ARMS
LEGACY_EXPERIMENT_REVISION = "legacy-unversioned"
