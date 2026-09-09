"""ReadCrop, fix, and patch stage plumbing for the Claude benchmark runner.

Split out of ``run-claude-agentic.py`` so the public runner stays under the 250 KB maintenance limit the suite enforces
on its Codex counterpart. The runner re-exports every name defined here, so ``patch.object`` against the runner module
keeps working and no caller needs to know which file a helper lives in.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shlex
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence
from _bench_common.claude_transport import parse_result_usage  # noqa: E402
from _bench_common.python_source import resolve_relative_base  # noqa: E402,F401
from _bench_common.agentic_contracts import (  # noqa: E402
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
)
from _bench_common.provider_parity_contracts import (  # noqa: E402
    canonical_task_hash,
    fresh_input_tokens,
    load_task_suite,
    prompt_hash,
    semantic_suite_hash,
    token_accounting_inconsistent,
)
from _bench_common.readcrop_contracts import (  # noqa: E402
    ReadcropUsage,
    build_readcrop_contract,
    parse_readcrop_answer,
    score_readcrop_answer,
)
from _bench_common.edit_patch_contracts import (  # noqa: E402
    EditTaskContract,
    FixMultiContract,
    FixSingleContract,
    StageIdentity,
    build_edit_task_contract,
    build_fix_multi_contract,
    build_fix_single_contract,
    stage_contract_sha256,
)


#: This module sits one level below the benchmarks directory, so every suite and manifest path is derived from
#: that parent rather than from this file's own directory.
BENCHMARKS_DIR = Path(__file__).resolve().parents[1]
#: Provenance rows record the public runner's bytes, not this module's, so the hash stays comparable across the
#: split that moved these stages out of it.
RUNNER_PATH = BENCHMARKS_DIR / "run-claude-agentic.py"

PARITY_MANIFEST_PATH = BENCHMARKS_DIR / "manifests" / "provider-parity-methodology.json"
READCROP_TASKS_PATH = BENCHMARKS_DIR / "suites" / "tasks-readcrop.json"
FIX_SINGLE_TASKS_PATH = BENCHMARKS_DIR / "suites" / "tasks-fix-single.json"
FIX_MULTI_TASKS_PATH = BENCHMARKS_DIR / "suites" / "tasks-fix-multi.json"
PATCH_TASKS_PATH = BENCHMARKS_DIR / "suites" / "tasks-patch.json"
READCROP_ARMS = ("A_plain", "B_auto", "C_strict")
FIX_SINGLE_ARMS = READCROP_ARMS
_READCROP_ANSWER_RE = re.compile(r"BEGIN_READ_CROP_JSON\s*(?P<payload>\{.*?\})\s*END_READ_CROP_JSON", re.DOTALL)
_FIX_SINGLE_QUERY_ARGUMENTS = {
    "FS-01": ("symbol", "EarlyStopping.__init__"),
    "FS-02": ("symbol", "EarlyStopping.__init__"),
    "FS-03": ("symbol", "ModelCheckpoint._save_checkpoint"),
    "FS-04": ("symbol", "ModelCheckpoint.__init__"),
}
_FIX_MULTI_QUERY_ARGUMENTS = {
    "FM-01": (
        "fn-rdeps",
        "lightning.pytorch.callbacks.early_stopping::EarlyStopping._run_early_stopping_check",
        "--exclude-tests",
    ),
    "FM-02": (
        "fn-rdeps",
        "lightning.pytorch.callbacks.model_checkpoint::ModelCheckpoint._save_checkpoint",
        "--exclude-tests",
    ),
    "FM-03": ("find-symbol", r"Strategy\.setup_environment$", "--exclude-tests", "--limit", "0"),
}
_PATCH_QUERY_ARGUMENTS = {
    "PT-01": ("symbol", "FitLoop.setup_data"),
    "PT-02": ("symbol", "DistributedSamplerWrapper"),
    "PT-03": ("symbol", "ThroughputMonitor._update"),
    "PT-04": ("symbol", "StochasticWeightAveraging.on_fit_start"),
    "PT-05": ("symbol", "_TrainingEpochLoop.advance"),
}


def _patch_index_path(repo_path: Path, task_id: str) -> Path:
    """Return the frozen historical index paired with one Patch baseline."""
    return repo_path / ".cache" / "codemap" / "patch" / f"{task_id}.json"


def _study_query_arguments(study: str) -> Mapping[str, tuple[str, ...]]:
    """Return the one canonical strict-query map for an executable study."""
    try:
        return {
            "fix-single": _FIX_SINGLE_QUERY_ARGUMENTS,
            "fix-multi": _FIX_MULTI_QUERY_ARGUMENTS,
            "patch": _PATCH_QUERY_ARGUMENTS,
        }[study]
    except KeyError as exc:
        raise ValueError(f"unsupported Claude executable study {study!r}") from exc


def _manifest_sha256(manifest_path: Path) -> str:
    """Return the exact provider-neutral manifest identity used by a scope."""
    try:
        return hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("provider-parity manifest is unavailable") from exc


def _readcrop_module_path(repo_path: Path, module: str) -> Path:
    """Resolve one source module using the frozen target's supported layouts."""
    relative = Path(*module.split("."))
    candidates = (repo_path / "src" / relative.with_suffix(".py"), repo_path / relative.with_suffix(".py"))
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise ValueError(f"read-crop module {module!r} is unavailable under {repo_path}")
    return path


def extract_readcrop_symbol_source(repo_path: Path, module: str, symbol: str) -> str:
    """Return exact AST source for one module-qualified function or method."""
    path = _readcrop_module_path(repo_path, module)
    text = path.read_text(encoding="utf-8")
    node: ast.AST = ast.parse(text)
    for part in symbol.split("."):
        node = next(
            (
                child
                for child in getattr(node, "body", [])
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == part
            ),
            None,
        )
        if node is None:
            raise ValueError(f"read-crop symbol {symbol!r} is unavailable in {path}")
    source = ast.get_source_segment(text, node)
    if not isinstance(source, str) or not source:
        raise ValueError(f"read-crop source is unavailable for {symbol!r}")
    return source


def load_claude_readcrop_tasks(
    repo_path: Path,
    tasks_path: Path = READCROP_TASKS_PATH,
    manifest_path: Path = PARITY_MANIFEST_PATH,
    selected_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Load the locked ReadCrop suite with source-anchored shared contracts."""
    raw_tasks = load_task_suite(tasks_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity manifest is unavailable or malformed") from exc
    suites = manifest.get("suites") if isinstance(manifest, Mapping) else None
    if not isinstance(suites, list):
        raise ValueError("provider-parity manifest requires suites")
    suite = next((item for item in suites if item.get("path") == "benchmarks/suites/tasks-readcrop.json"), None)
    if not isinstance(suite, Mapping):
        raise ValueError("provider-parity manifest lacks the read-crop suite")
    if suite.get("ordered_task_ids") != [task["id"] for task in raw_tasks]:
        raise ValueError("read-crop task order drifted")
    if suite.get("semantic_suite_sha256") != semantic_suite_hash(raw_tasks):
        raise ValueError("read-crop suite identity drifted")
    rows = {row.get("id"): row for row in suite.get("tasks", []) if isinstance(row, Mapping)}
    wanted = set(selected_ids or [str(task["id"]) for task in raw_tasks])
    loaded: list[dict[str, Any]] = []
    for task in raw_tasks:
        if task["id"] not in wanted:
            continue
        row = rows.get(task["id"])
        if not isinstance(row, Mapping) or row.get("canonical_task_sha256") != canonical_task_hash(task):
            raise ValueError(f"read-crop task identity drifted for {task['id']}")
        if row.get("prompt_sha256") != prompt_hash(task):
            raise ValueError(f"read-crop prompt identity drifted for {task['id']}")
        source = extract_readcrop_symbol_source(repo_path, str(task["primary_module"]), str(task["symbol"]))
        loaded.append({"task": task, "source": source, "contract": build_readcrop_contract(task, source=source)})
    if {item["contract"].task_id for item in loaded} != wanted:
        raise ValueError("--tasks must select known read-crop task IDs")
    return loaded


def readcrop_prompt(arm: str, task: Mapping[str, Any]) -> str:
    """Build the shared strict answer envelope with an arm-only tool supplement."""
    symbol = str(task["symbol"])
    supplements = {
        "A_plain": "Codemap is absent and inaccessible. Use ordinary repository tools.",
        "B_auto": "Codemap is installed and available through /codemap-py:query-code; use it when useful.",
        "C_strict": (
            "Codemap is installed. Before reading source, invoke the exact Skill query "
            f"`/codemap-py:query-code symbol {symbol}`. The benchmark verifies its completed frozen CLI query."
        ),
    }
    try:
        supplement = supplements[arm]
    except KeyError as exc:
        raise ValueError(f"unsupported Claude read-crop arm {arm!r}") from exc
    parameter_requirement = (
        "each exact required source parameter"
        if task.get("required_parameters") is not None
        else "every exact source parameter"
    )
    envelope = (
        "After completing any tools, return no prose or Markdown outside this exact envelope:\n"
        "BEGIN_READ_CROP_JSON\n"
        '{"signature":"full qualified signature","parameters":["exact parameter name"],"behavior":"non-empty contract summary"}\n'
        "END_READ_CROP_JSON\n"
        "The JSON object must have exactly those three fields. "
        f"`parameters` must list {parameter_requirement} name, and `behavior` must be a non-empty summary."
    )
    return f"{supplement}\n\n{task['prompt']}\n\n{envelope}"


def _tool_result_text(content: Any) -> str:
    """Return one Claude tool result as plain text for success classification."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(item.get("text", "")) if isinstance(item, Mapping) else str(item) for item in content)
    return str(content)


def _claude_message_blocks(event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return structured chat blocks without interpreting diagnostic messages as tool evidence.

    Native events also carry scalar diagnostic messages. Keep raw events intact; only assistant/user objects with list-
    valued content can supply tool blocks.
    """
    if event.get("type") not in ("assistant", "user"):
        return []
    message = event.get("message")
    if not isinstance(message, Mapping):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, Mapping)]


def _query_arguments_from_bash(command: str) -> tuple[str, ...] | None:
    """Return canonical Codemap query arguments from one executable Bash command.

    The decision-grade treatment credits only the stable PATH command ``codemap-py query ...``, its installable absolute
    launcher, or the legacy ``scan-query ...`` launcher. A Skill invocation remains insufficient until its underlying
    CLI command completes against the frozen checkout.
    """
    query_tail = _query_command_tail(command)
    return _command_arguments(query_tail[0]) if query_tail is not None else None


def _query_command_tail(command: str) -> tuple[str, int] | None:
    """Return one recognized query tail and its command boundary.

    The end coordinate preserves whether a succeeding shell segment could have replaced a failed query's output before
    Claude reported the Bash result.
    """
    boundary = r"(?:^|&&|\|\||;|\|)\s*"
    environment = r"(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    launcher = (
        r"(?:codemap-py|/[^\s'\"`|;&()<>]*/bin/codemap-py|\$\{CLAUDE_PLUGIN_ROOT:-plugins/codemap-py\}/bin/codemap-py)"
    )
    command_token = rf'(?:"{launcher}"|{launcher})'
    canonical = re.search(rf"{boundary}{environment}{command_token}\s+query\s+([^\n;&|]+)", command)
    if canonical is not None:
        return canonical.group(1), canonical.end()
    legacy = re.search(rf"{boundary}{environment}(?:\S*/)?scan-query\s+([^\n;&|]+)", command)
    return (legacy.group(1), legacy.end()) if legacy is not None else None


def _command_arguments(value: str) -> tuple[str, ...]:
    """Drop shell-only redirections from one already-isolated command tail."""
    return tuple(token for token in shlex.split(value) if token != "--compact" and not re.match(r"(?:\d?>|>&)", token))


def _absolute_codemap_launchers(command: str) -> set[PurePosixPath]:
    """Return absolute plugin launchers that are permitted outside one worktree.

    Args:
        command: One recorded Bash command from the transcript.

    Returns:
        The launcher paths exactly as the agent named them, normalized only lexically.

    Examples:
        >>> sorted(str(path) for path in _absolute_codemap_launchers("/opt/cm/bin/codemap-py query symbol X"))
        ['/opt/cm/bin/codemap-py']
    """
    return {
        PurePosixPath(path)
        for path in re.findall(r"(?<![A-Za-z0-9_.-])(/[^\s'\"`|;&()<>]*/bin/codemap-py)(?=\"?\s+query\b)", command)
    }


def _workspace_containment_roots(workspace_root: Path) -> tuple[PurePosixPath, ...]:
    """Return the POSIX forms a transcript path may use to name one checkout.

    A transcript records the path the agent typed, so a checkout reachable through a
    symlinked temp directory is named either way. Both forms are containment roots;
    resolving the *observed* path against this host instead would be wrong everywhere and
    catastrophic on Windows, where a leading-slash path acquires the current drive letter.

    Args:
        workspace_root: The disposable checkout handed to the agent.

    Returns:
        Deduplicated POSIX-form roots, longest-lived form first.

    Examples:
        >>> from pathlib import PurePosixPath, PureWindowsPath
        >>> _workspace_containment_roots(PureWindowsPath(r"D:\\a\\repo"))
        (PurePosixPath('D:/a/repo'),)
    """
    forms = [workspace_root]
    resolve = getattr(workspace_root, "resolve", None)
    if resolve is not None:
        forms.append(resolve())
    return tuple(dict.fromkeys(PurePosixPath(form.as_posix()) for form in forms))


def _is_inside_workspace(observed: PurePosixPath, roots: Sequence[PurePosixPath]) -> bool:
    """Return whether one observed path names something inside the disposable checkout.

    Args:
        observed: Absolute path exactly as the transcript recorded it.
        roots: Containment roots from :func:`_workspace_containment_roots`.

    Returns:
        Whether the observed path is the checkout or lives beneath it.

    Examples:
        >>> from pathlib import PurePosixPath
        >>> roots = (PurePosixPath("/opt/codemap-py"),)
        >>> _is_inside_workspace(PurePosixPath("/opt/codemap-py/bin/x"), roots)
        True
        >>> _is_inside_workspace(PurePosixPath("/opt/codemap-py-evil/bin/x"), roots)
        False
    """
    return any(observed == root or observed.is_relative_to(root) for root in roots)


def _tool_input_strings(value: Any) -> Iterator[str]:
    """Yield string leaves from a native Claude tool-input object."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _tool_input_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _tool_input_strings(nested)


def _outside_workspace_path_evidence(
    events: Sequence[Mapping[str, Any]], workspace_root: Path | None
) -> tuple[list[str], list[str]]:
    """Return attempted and successful absolute accesses outside the checkout.

    The harness may safely expose the disposable checkout by absolute path, but only a successful external access can
    leak source bytes into an answer. Denied guesses remain diagnostic evidence without quarantining a clean cell. Only
    tool fields that execute a command or name a filesystem target count; written content is data rather than an access
    request.

    Every path here is evidence about the agent's filesystem, recorded verbatim: it is classified against the checkout
    lexically and never resolved against the host running the scorer.
    """
    if workspace_root is None:
        return [], []
    roots = _workspace_containment_roots(workspace_root)
    benign_shell_endpoints = {PurePosixPath("/dev/null"), PurePosixPath("/dev/stdout"), PurePosixPath("/dev/stderr")}
    attempted: list[str] = []
    successful: list[str] = []
    attempted_seen: set[str] = set()
    successful_seen: set[str] = set()
    pending: dict[str, list[str]] = {}
    for event in events:
        content = _claude_message_blocks(event)
        if event.get("type") == "assistant":
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                    continue
                tool_input = block.get("input")
                if not isinstance(tool_input, Mapping):
                    continue
                command = str(tool_input.get("command", "")) if block.get("name") == "Bash" else ""
                allowed_launchers = _absolute_codemap_launchers(command)
                path_values = (
                    (command,)
                    if command
                    else tuple(
                        str(tool_input[field])
                        for field in ("file_path", "path", "pattern")
                        if isinstance(tool_input.get(field), str)
                    )
                )
                block_paths: list[str] = []
                for value in path_values:
                    variable_launchers = [
                        match.span()
                        for match in re.finditer(
                            re.escape("${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/codemap-py"), value
                        )
                    ]
                    for path_match in re.finditer(r"(?<![A-Za-z0-9_.-])/(?:[^\s'\"`|;&()<>]+)", value):
                        # A slash after a glob or shell expansion terminator continues a relative token.
                        if path_match.start() and value[path_match.start() - 1] in "*?]})":
                            continue
                        raw_path = path_match.group()
                        if any(
                            start <= path_match.start() and path_match.end() <= end for start, end in variable_launchers
                        ):
                            continue
                        candidate = PurePosixPath(raw_path)
                        if (
                            candidate in benign_shell_endpoints
                            or candidate in allowed_launchers
                            or _is_inside_workspace(candidate, roots)
                        ):
                            continue
                        normalized = str(candidate)
                        if normalized not in attempted_seen:
                            attempted_seen.add(normalized)
                            attempted.append(normalized)
                        if normalized not in block_paths:
                            block_paths.append(normalized)
                if block_paths:
                    pending[str(block.get("id", ""))] = block_paths
        elif event.get("type") == "user":
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                    continue
                paths = pending.pop(str(block.get("tool_use_id", "")), [])
                result_text = _tool_result_text(block.get("content", ""))
                if block.get("is_error") or "<tool_use_error>" in result_text:
                    continue
                for normalized in paths:
                    if normalized in successful_seen:
                        continue
                    successful_seen.add(normalized)
                    successful.append(normalized)
    return attempted, successful


def _frozen_index_recovery_attempted(events: Sequence[Mapping[str, Any]]) -> bool:
    """Return whether native tool input tried to rebuild the frozen index."""
    recovery = re.compile(r"(?:\bcodemap-py\s+(?:scan|index)\b|\bscan-index\b|\bscan\s+--incremental\b)")
    for event in events:
        if event.get("type") != "assistant":
            continue
        content = _claude_message_blocks(event)
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                continue
            if any(recovery.search(value) for value in _tool_input_strings(block.get("input", {}))):
                return True
    return False


def _claude_codemap_evidence(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Count completed underlying Codemap CLI queries, never wrapper launches.

    A successful Claude ``Skill`` result only proves that the wrapper ran; it does not prove that its nested command
    accessed the frozen index. Canonical C-strict evidence therefore requires a matching successful Bash result for
    ``codemap-py query`` or legacy ``scan-query``.
    """
    pending: dict[str, tuple[tuple[str, ...], bool, bool]] = {}
    observed = 0
    skill_launches = 0
    query_skill_launches = 0
    successful_arguments: list[list[str]] = []
    compact_successful_arguments: list[list[str]] = []
    for event in events:
        content = _claude_message_blocks(event)
        if event.get("type") == "assistant":
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                tool_input = block.get("input")
                if not isinstance(tool_input, Mapping):
                    continue
                if name == "Skill" and "codemap" in str(tool_input.get("skill", "")):
                    skill_launches += 1
                    if str(tool_input.get("skill", "")) == "codemap-py:query-code":
                        query_skill_launches += 1
                command = str(tool_input.get("command", ""))
                query_tail = _query_command_tail(command) if name == "Bash" else None
                if query_tail is not None:
                    tail, query_end = query_tail
                    observed += 1
                    try:
                        arguments = _command_arguments(tail)
                    except ValueError:
                        pending[str(block.get("id", ""))] = ((), False, False)
                        continue
                    terminal = not command[query_end:].strip()
                    pending[str(block.get("id", ""))] = (
                        arguments,
                        terminal and _is_compact_query(tail, arguments),
                        terminal,
                    )
        elif event.get("type") == "user":
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                    continue
                pending_query = pending.pop(str(block.get("tool_use_id", "")), None)
                if pending_query is None:
                    continue
                arguments, compact, terminal = pending_query
                result_text = _tool_result_text(block.get("content", ""))
                if terminal and _native_tool_result_succeeded(block, result_text):
                    successful_arguments.append(list(arguments))
                    if compact and _compact_query_result_succeeded(result_text):
                        compact_successful_arguments.append(list(arguments))
    return {
        "codemap_calls": observed,
        "codemap_successful_calls": len(successful_arguments),
        "codemap_query_attempted": observed,
        "codemap_query_succeeded": len(successful_arguments),
        "codemap_compact_success": bool(compact_successful_arguments),
        "compact_successful_query_arguments": compact_successful_arguments,
        "codemap_skill_launches": skill_launches,
        "codemap_query_skill_launches": query_skill_launches,
        "successful_query_arguments": successful_arguments,
    }


def _is_compact_query(query_tail: str, arguments: tuple[str, ...]) -> bool:
    """Return whether one recognized query segment is compact and task-shaped."""
    try:
        tokens = shlex.split(query_tail)
    except ValueError:
        return False
    if "--compact" not in tokens:
        return False
    position = 0
    options_with_values = {"--index", "--limit", "--root", "--timeout"}
    while position < len(arguments) and arguments[position].startswith("-"):
        position += 2 if arguments[position] in options_with_values else 1
    return len(arguments) - position >= 2


def _native_tool_result_succeeded(block: Mapping[str, Any], result_text: str) -> bool:
    """Return whether Claude's own matching tool result records a successful command exit."""
    return (
        not bool(block.get("is_error"))
        and "<tool_use_error>" not in result_text
        and not re.search(r"(?:^|\n)(?:Exit code|Command failed)\b", result_text, flags=re.IGNORECASE)
    )


def _compact_query_result_succeeded(result_text: str) -> bool:
    """Return whether a completed compact query emitted one parseable native JSON payload."""
    try:
        payload = json.loads(result_text)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, list) or (isinstance(payload, dict) and "error" not in payload)


def parse_claude_readcrop_events(
    events: Sequence[Mapping[str, Any]], *, arm: str, contract: Any, workspace_root: Path | None = None
) -> dict[str, Any]:
    """Normalize Claude stream-json events without estimating unavailable tool payload tokens."""
    if arm not in READCROP_ARMS:
        raise ValueError(f"unsupported Claude read-crop arm {arm!r}")
    summary = _claude_event_summary(events)
    output_text = summary["output_text"]
    native_usage = summary.pop("usage")
    match = _READCROP_ANSWER_RE.search(output_text)
    answer_error = ""
    score = None
    if match is None:
        answer_error = "missing strict read-crop answer envelope"
    else:
        try:
            score = score_readcrop_answer(contract, parse_readcrop_answer(match.group("payload")))
        except ValueError as exc:
            answer_error = str(exc)
    tool_result_tokens = None
    ReadcropUsage(native_usage.input_tokens, tool_result_tokens)
    codemap = _claude_codemap_evidence(events)
    codemap_calls = int(codemap["codemap_calls"])
    codemap_successful_calls = int(codemap["codemap_successful_calls"])
    attempted_outside_paths, outside_paths = _outside_workspace_path_evidence(events, workspace_root)
    recovery_attempted = _frozen_index_recovery_attempted(events)
    contaminated = bool(
        (arm == "A_plain" and (codemap_calls > 0 or int(codemap["codemap_skill_launches"]) > 0))
        or outside_paths
        or recovery_attempted
    )
    strict_query = None if arm != "C_strict" else ["symbol", contract.symbol] in codemap["successful_query_arguments"]
    compliance = {
        "A_plain": not contaminated,
        "B_auto": True,
        "C_strict": bool(codemap["codemap_query_skill_launches"])
        and bool(codemap_successful_calls)
        and bool(strict_query),
    }[arm]
    return {
        "task_id": contract.task_id,
        "arm": arm,
        "success": native_usage.success and not answer_error and compliance and not contaminated,
        "answer_error": answer_error,
        "primary_correct": score.primary_correct if score is not None else False,
        "quality_score": score.quality_score if score is not None else None,
        "quality_components": dict(score.quality_components) if score is not None else {},
        # POLICY — unscoreable cell: every recall field is None, none is zero.
        # Previously an unparsable answer wrote 0.0 into parameter_recall and
        # keyword_recall_diagnostic but None into the two behavior fields, so the
        # same failure was averaged INTO two means and omitted FROM the other two.
        # None is now uniform, matching quality_score/quality_components in this
        # same row: a 0.0 recall asserts a measurement that never happened, while
        # None says the answer could not be scored at all. None therefore means
        # "not scoreable OR not applicable"; `answer_error` and `quality_score`
        # disambiguate the two, and `success`/`primary_correct` (both False here)
        # carry the failure so it is never mistaken for a passing cell.
        # Downstream means must report the unscoreable count alongside the mean.
        # Mirrors the Codex lane (_bench_codex/stage_readcrop.py) so the two
        # providers cannot disagree on what a failed cell means.
        "parameter_recall": score.parameter_recall if score is not None else None,
        "behavior_fact_recall": score.behavior_fact_recall if score is not None else None,
        "behavior_facts_correct": score.behavior_facts_correct if score is not None else None,
        "keyword_recall_diagnostic": score.keyword_recall if score is not None else None,
        "input_tokens": native_usage.input_tokens,
        "cache_creation_tokens": native_usage.cache_creation_tokens,
        "cache_read_tokens": native_usage.cache_read_tokens,
        "cached_input_tokens": native_usage.cache_creation_tokens + native_usage.cache_read_tokens,
        "fresh_input_tokens": fresh_input_tokens(
            native_usage.input_tokens, native_usage.cache_creation_tokens + native_usage.cache_read_tokens
        ),
        "token_accounting_inconsistent": token_accounting_inconsistent(
            native_usage.input_tokens, native_usage.cache_creation_tokens + native_usage.cache_read_tokens
        ),
        "output_tokens": native_usage.output_tokens,
        "tool_result_tokens": tool_result_tokens,
        "command_calls": summary["command_calls"],
        "codemap_calls": codemap_calls,
        "codemap_successful_calls": codemap_successful_calls,
        "codemap_skill_launches": codemap["codemap_skill_launches"],
        "codemap_query_skill_launches": codemap["codemap_query_skill_launches"],
        "codemap_attempted": codemap_calls > 0,
        "codemap_used": codemap_successful_calls > 0,
        "codemap_query_attempted": codemap["codemap_query_attempted"],
        "codemap_query_succeeded": codemap["codemap_query_succeeded"],
        "codemap_compact_success": codemap["codemap_compact_success"],
        "successful_query_arguments": codemap["successful_query_arguments"],
        "strict_query_conformance": strict_query,
        "compliance": compliance,
        "contaminated": contaminated,
        "attempted_outside_workspace_paths": attempted_outside_paths,
        "outside_workspace_paths": outside_paths,
        "frozen_index_recovery_attempted": recovery_attempted,
        "pooling_eligible": bool(native_usage.success and not answer_error and compliance and not contaminated),
        "native_subtype": native_usage.subtype,
        **summary,
        "provider_binding": dict(contract.provider_binding()),
    }


def resolve_readcrop_scope(
    tasks: Sequence[Mapping[str, Any]],
    manifest_path: Path = PARITY_MANIFEST_PATH,
    tasks_path: Path = READCROP_TASKS_PATH,
) -> dict[str, Any]:
    """Return the deterministic source-bound no-model Claude ReadCrop scope."""
    task_ids = [str(item["contract"].task_id) for item in tasks]
    if not task_ids or len(set(task_ids)) != len(task_ids):
        raise ValueError("Claude read-crop scope requires unique selected task IDs")
    payload: dict[str, Any] = {
        "provider": "claude",
        "study": "readcrop",
        "manifest_sha256": _manifest_sha256(manifest_path),
        "suite_sha256": hashlib.sha256(tasks_path.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(RUNNER_PATH.read_bytes()).hexdigest(),
        "task_ids": task_ids,
        "arms": list(READCROP_ARMS),
        "repetitions": 1,
        "total_cells": len(tasks) * len(READCROP_ARMS),
        "source_contracts": {
            item["contract"].task_id: {
                "oracle_sha256": item["contract"].oracle_sha256,
                "source_sha256": item["contract"].source_sha256,
            }
            for item in tasks
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**payload, "scope_sha256": hashlib.sha256(encoded).hexdigest()}


def _load_claude_fix_tasks(
    *,
    study: str,
    tasks_path: Path,
    manifest_path: Path,
    selected_ids: Sequence[str] | None,
    contract_builder: Callable[[Mapping[str, Any]], FixSingleContract | FixMultiContract | EditTaskContract],
) -> list[dict[str, Any]]:
    """Load one canonical fix suite while preserving its manifest identity."""
    raw_tasks = load_task_suite(tasks_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity manifest is unavailable or malformed") from exc
    suites = manifest.get("suites") if isinstance(manifest, Mapping) else None
    if not isinstance(suites, list):
        raise ValueError("provider-parity manifest requires suites")
    relative_suite_path = f"benchmarks/suites/tasks-{study}.json"
    suite = next((item for item in suites if item.get("path") == relative_suite_path), None)
    if not isinstance(suite, Mapping):
        raise ValueError(f"provider-parity manifest lacks the {study} suite")
    if suite.get("ordered_task_ids") != [task["id"] for task in raw_tasks]:
        raise ValueError(f"{study} task order drifted")
    if suite.get("semantic_suite_sha256") != semantic_suite_hash(raw_tasks):
        raise ValueError(f"{study} suite identity drifted")
    rows = {row.get("id"): row for row in suite.get("tasks", []) if isinstance(row, Mapping)}
    wanted = set(selected_ids or [str(task["id"]) for task in raw_tasks])
    loaded: list[dict[str, Any]] = []
    for task in raw_tasks:
        if task["id"] not in wanted:
            continue
        row = rows.get(task["id"])
        if not isinstance(row, Mapping) or row.get("canonical_task_sha256") != canonical_task_hash(task):
            raise ValueError(f"{study} task identity drifted for {task['id']}")
        if row.get("prompt_sha256") != prompt_hash(task):
            raise ValueError(f"{study} prompt identity drifted for {task['id']}")
        loaded.append({"task": task, "contract": contract_builder(task)})
    if {item["contract"].task_id for item in loaded} != wanted:
        raise ValueError(f"--tasks must select known {study} task IDs")
    return loaded


def load_claude_fix_single_tasks(
    tasks_path: Path = FIX_SINGLE_TASKS_PATH,
    manifest_path: Path = PARITY_MANIFEST_PATH,
    selected_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Load canonical Fix-Single tasks with the provider-neutral contract owner."""
    return _load_claude_fix_tasks(
        study="fix-single",
        tasks_path=tasks_path,
        manifest_path=manifest_path,
        selected_ids=selected_ids,
        contract_builder=build_fix_single_contract,
    )


def load_claude_fix_multi_tasks(
    tasks_path: Path = FIX_MULTI_TASKS_PATH,
    manifest_path: Path = PARITY_MANIFEST_PATH,
    selected_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Load canonical Fix-Multi tasks with the provider-neutral contract owner."""
    return _load_claude_fix_tasks(
        study="fix-multi",
        tasks_path=tasks_path,
        manifest_path=manifest_path,
        selected_ids=selected_ids,
        contract_builder=build_fix_multi_contract,
    )


def _patch_stage_identity(tasks_path: Path, contracts: Sequence[EditTaskContract]) -> StageIdentity:
    """Bind Claude Patch evidence to the selected suite and shared scorer bytes."""
    return StageIdentity(
        stage="patch",
        revision="provider-parity-patch-v1",
        task_suite_sha256=hashlib.sha256(tasks_path.read_bytes()).hexdigest(),
        contract_sha256=stage_contract_sha256(contracts),
    )


def load_claude_patch_tasks(
    tasks_path: Path = PATCH_TASKS_PATH,
    manifest_path: Path = PARITY_MANIFEST_PATH,
    selected_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Load historical Patch tasks with their provider-neutral stage identity."""
    loaded = _load_claude_fix_tasks(
        study="patch",
        tasks_path=tasks_path,
        manifest_path=manifest_path,
        selected_ids=selected_ids,
        contract_builder=build_edit_task_contract,
    )
    contracts = [item["contract"] for item in loaded]
    if not all(isinstance(contract, EditTaskContract) for contract in contracts):
        raise RuntimeError("patch task loader did not construct EditTaskContract values")
    identity = _patch_stage_identity(tasks_path, contracts)
    for item in loaded:
        contract = item["contract"]
        assert isinstance(contract, EditTaskContract)
        item["stage_identity"] = identity
        item["provider_binding"] = dict(contract.scientific_field_hashes(identity))
    return loaded


def _provider_binding(item: Mapping[str, Any]) -> Mapping[str, str]:
    """Return the immutable provider fields carried by one stage task."""
    binding = item.get("provider_binding")
    if isinstance(binding, Mapping):
        return {str(key): str(value) for key, value in binding.items()}
    return item["contract"].provider_binding()


def _resolve_claude_fix_scope(
    *, study: str, tasks: Sequence[Mapping[str, Any]], manifest_path: Path, tasks_path: Path
) -> dict[str, Any]:
    """Bind one Claude fix suite to provider-neutral task contracts."""
    task_ids = [str(item["contract"].task_id) for item in tasks]
    if not task_ids or len(set(task_ids)) != len(task_ids):
        raise ValueError(f"Claude {study} scope requires unique selected task IDs")
    payload: dict[str, Any] = {
        "provider": "claude",
        "study": study,
        "manifest_sha256": _manifest_sha256(manifest_path),
        "suite_sha256": hashlib.sha256(tasks_path.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(RUNNER_PATH.read_bytes()).hexdigest(),
        "task_ids": task_ids,
        "arms": list(FIX_SINGLE_ARMS),
        "repetitions": 1,
        "total_cells": len(tasks) * len(FIX_SINGLE_ARMS),
        "contracts": {item["contract"].task_id: dict(_provider_binding(item)) for item in tasks},
    }
    if study == "patch":
        payload["historical_baselines"] = {item["contract"].task_id: item["contract"].baseline_commit for item in tasks}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**payload, "scope_sha256": hashlib.sha256(encoded).hexdigest()}


def resolve_claude_fix_single_scope(
    tasks: Sequence[Mapping[str, Any]],
    manifest_path: Path = PARITY_MANIFEST_PATH,
    tasks_path: Path = FIX_SINGLE_TASKS_PATH,
) -> dict[str, Any]:
    """Bind Claude planning to the shared Fix-Single science contract."""
    return _resolve_claude_fix_scope(
        study="fix-single", tasks=tasks, manifest_path=manifest_path, tasks_path=tasks_path
    )


def resolve_claude_fix_multi_scope(
    tasks: Sequence[Mapping[str, Any]],
    manifest_path: Path = PARITY_MANIFEST_PATH,
    tasks_path: Path = FIX_MULTI_TASKS_PATH,
) -> dict[str, Any]:
    """Bind Claude planning to the shared Fix-Multi science contract."""
    return _resolve_claude_fix_scope(study="fix-multi", tasks=tasks, manifest_path=manifest_path, tasks_path=tasks_path)


def resolve_claude_patch_scope(
    tasks: Sequence[Mapping[str, Any]],
    manifest_path: Path = PARITY_MANIFEST_PATH,
    tasks_path: Path = PATCH_TASKS_PATH,
) -> dict[str, Any]:
    """Bind Claude Patch selection to each task's historical immutable contract."""
    return _resolve_claude_fix_scope(study="patch", tasks=tasks, manifest_path=manifest_path, tasks_path=tasks_path)


def _claude_event_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Normalize provider-native usage, assistant text, and raw event identity."""
    usage = None
    partial_input = 0
    partial_cache_creation = 0
    partial_cache_read = 0
    seen_message_ids: set[str] = set()
    output_text = ""
    command_calls = 0
    for event in events:
        if event.get("type") == "result":
            usage = parse_result_usage(dict(event))
        message = event.get("message", {})
        if event.get("type") != "assistant" or not isinstance(message, Mapping):
            continue
        content = message.get("content", [])
        message_id = message.get("id")
        native_usage = message.get("usage")
        if isinstance(message_id, str) and message_id not in seen_message_ids and isinstance(native_usage, Mapping):
            seen_message_ids.add(message_id)
            partial_input += int(native_usage.get("input_tokens", 0))
            partial_cache_creation += int(native_usage.get("cache_creation_input_tokens", 0))
            partial_cache_read += int(native_usage.get("cache_read_input_tokens", 0))
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                output_text += str(block["text"])
            elif block.get("type") == "tool_use":
                command_calls += 1
    usage_complete = usage is not None
    if usage is None:
        usage = parse_result_usage(
            {
                "usage": {
                    "input_tokens": partial_input,
                    "cache_creation_input_tokens": partial_cache_creation,
                    "cache_read_input_tokens": partial_cache_read,
                }
            }
        )
    raw_events = [dict(event) for event in events]
    return {
        "usage": usage,
        "usage_complete": usage_complete,
        "usage_source": "result" if usage_complete else ("partial_stream" if usage.input_tokens else "unavailable"),
        "output_text": output_text,
        "command_calls": command_calls,
        "raw_events": raw_events,
        "raw_events_sha256": hashlib.sha256(
            json.dumps(raw_events, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
