"""Validate native reviewer execution against retained context and scheduling evidence.

## Purpose

Bind native review claims to their actual runtime, complete source reads, immutable role cards, and parent scheduling
receipts. Keep this evidence boundary separate from result presentation and merge-decision validation.

## Scope

Own the native and local reviewer execution, inspection-plan, context-read, capacity, scheduling, recovery, and
instruction-boundary checks used by code-review. Retain historical reader identities and exact diagnostics. Evidence
loaders resolve contained run artifacts, parse rollout records, and hash the bytes consumed by these checks.

## Usage

Imported by ``validate_artifacts.py`` after the skill and plugin shared directories are available on the import path.
The validator reexports the existing helper names for review preparation and batch consumers; this module has no CLI.

## Outputs

Return validated evidence or derived values to the calling validator. Read local artifact files and runtime receipts; do
not rewrite artifacts, launch reviewers, or mutate the repository.

## Failure

Raise ``SystemExit`` with the existing diagnostic when evidence violates a contract. Filesystem and JSON errors retain
their existing propagation, allowing the owning workflow to stop instead of silently accepting incomplete evidence.

## Used by

Code-review artifact validation, review preparation, batch assembly, and their regression tests consume these helpers
through the existing validator entrypoint. Result schemas and command-line behavior remain owned by that entrypoint.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from local_reviewer_wave import ReviewRouteError
from local_reviewer_wave import validate_evidence as validate_local_reviewer_evidence
from parallel_execution import _SECRET_PATTERNS, validate_inspection_contexts
from review_context import context_pages, dispatch_message, render_read_call, render_read_output

#: Severity values a reviewer finding record may use.
FINDING_SEVERITIES = ("critical", "high", "medium", "low")

#: Attempt error types treated as transient, so a failed attempt may be retried.
TRANSIENT_RETRY_ERRORS = {"rate_limited", "timeout", "transport_error"}


#: Directory containing the code-review skill; its review_context.py is hashed for manifest checks.
SKILL_DIRECTORY = Path(__file__).resolve().parent
#: Root of the Codex Rig plugin, used to find the role cards directory.
PLUGIN_ROOT = SKILL_DIRECTORY.parents[1]

#: Roles that, when routed, must have an independent review pass recorded.
REQUIRED_ROLES = {"qa-specialist", "challenger"}


#: SHA-256 of the review_context.py reader shipped with protocol 6, accepted in older manifests.
LEGACY_PROTOCOL_V6_READER_SHA256 = "bf0025b22283b98a95e6a77a08b600e090f417e10bb0c90f3371bf0d19198132"


#: SHA-256 of the earlier single-call context reader, accepted in older manifests.
LEGACY_SINGLE_CALL_READER_SHA256 = "47024ba02c7dec6927356dca33ec44e9710de325fcc1fbb8ec56f66ad0a9c772"


#: SHA-256 of the earlier all-page context reader, accepted in older manifests.
LEGACY_ALL_PAGE_READER_SHA256 = "c185dc007a261a2d9c0e449e5a888a7a771cd99336ebe8c7085432bc2b0d43c8"


#: Digests of every earlier reader that manifests with a work-directory context may still record.
LEGACY_WORKDIR_READER_SHA256S = frozenset(
    {LEGACY_PROTOCOL_V6_READER_SHA256, LEGACY_SINGLE_CALL_READER_SHA256, LEGACY_ALL_PAGE_READER_SHA256}
)


def _load_role_card(roles_dir: Path, role: str) -> dict[str, str]:
    """Load a role-card contract and bind it to its exact bytes."""
    path = roles_dir / role / "ROLE.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SystemExit(f"role-card-missing:{role}") from error
    if not lines or lines[0] != "---":
        raise SystemExit(f"role-card-frontmatter-invalid:{role}")
    try:
        closing_index = lines.index("---", 1)
    except ValueError as error:
        raise SystemExit(f"role-card-frontmatter-invalid:{role}") from error
    fields: dict[str, str] = {}
    for line in lines[1:closing_index]:
        key, separator, value = line.partition(":")
        if not separator or not key or not value.strip() or key in fields:
            raise SystemExit(f"role-card-frontmatter-invalid:{role}")
        fields[key] = value.strip()
    required = ("role_id", "model", "model_reasoning_effort", "approval_policy", "sandbox_mode")
    if fields.get("role_id") != role or any(field not in fields for field in required):
        raise SystemExit(f"role-card-contract-invalid:{role}")
    return {
        "role_id": role,
        "role_card_sha256": _sha256(path),
        "model": fields["model"],
        "model_reasoning_effort": fields["model_reasoning_effort"],
        "approval_policy": fields["approval_policy"],
        "sandbox_mode": fields["sandbox_mode"],
    }


def _load_json(path: Path) -> dict[str, Any]:
    """Read one required JSON object from a review evidence artifact."""
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit(f"expected JSON object: {path}")
    return payload


def _resolve_path(out_dir: Path, raw_path: object) -> Path:
    """Resolve one declared review artifact path without consulting the caller's working directory.

    The recorded path was written by an earlier process whose directory is not stored alongside it, so probing the
    reader's own directory made a finished review valid in one place and invalid in another. Candidates are derived from
    `out_dir` — the current relative form first, then its ancestors for runs written before that convention — and the
    containment check below still rejects anything landing outside the review output.
    """
    if not isinstance(raw_path, str) or not raw_path:
        raise SystemExit("missing output path")
    declared = Path(raw_path)
    path = declared if declared.is_absolute() else out_dir / declared
    if not declared.is_absolute() and not path.is_file():
        for ancestor in out_dir.resolve().parents:
            if (ancestor / declared).is_file():
                path = ancestor / declared
                break
    resolved = path.resolve()
    if not resolved.is_relative_to(out_dir.resolve()):
        raise SystemExit(f"artifact-path-outside-review-output:{raw_path}")
    return resolved


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest for an evidence file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read valid object rows from a Codex rollout log."""
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _find_rollout(codex_home: Path, thread_id: str) -> Path:
    """Find the unique rollout log for a Codex thread ID."""
    matches = list((codex_home / "sessions").rglob(f"*{thread_id}*.jsonl"))
    if len(matches) != 1:
        raise SystemExit(f"provenance-rollout-count:{thread_id}:{len(matches)}")
    return matches[0]


def _event_payloads(rows: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    """Select event-message payloads of one type."""
    payloads = [
        row["payload"]
        for row in rows
        if row.get("type") == "event_msg"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type") == event_type
    ]
    if event_type != "sub_agent_activity":
        return payloads
    for row in rows:
        payload = row.get("payload")
        if row.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        item = payload.get("item")
        if payload.get("type") != "item_completed" or not isinstance(item, dict):
            continue
        if item.get("type") != "SubAgentActivity":
            continue
        payloads.append(
            {
                "event_id": item.get("id"),
                "kind": item.get("kind"),
                "agent_path": item.get("agent_path"),
                "agent_thread_id": item.get("agent_thread_id"),
                "started_at_ms": payload.get("started_at_ms"),
                "completed_at_ms": payload.get("completed_at_ms"),
            }
        )
    return payloads


def _paged_native_manifest(manifest: dict[str, Any]) -> bool:
    """Identify audited native page reads while keeping aggregate admission separate."""
    return manifest.get("schema_version") == 6 or (
        manifest.get("schema_version") in {7, 8} and manifest.get("manifest_kind") == "native-wave"
    )


def _native_dispatch_message(manifest: dict[str, Any], plan_path: Path, role: str, attempt: int) -> str:
    """Render the declared native recipe, preserving historical reader paths exactly."""
    message = dispatch_message(
        _native_recipe_plan(manifest, plan_path),
        role,
        attempt,
        manifest["context_reader_python"],
        provenance_header=manifest.get("dispatch_protocol", "paged-context-v6") == "paged-context-v6",
        reader_path=Path(manifest["context_reader_path"]) if manifest.get("schema_version") in {7, 8} else None,
        _all_page_calls=manifest.get("schema_version") != 6
        and manifest.get("context_reader_sha256")
        not in {LEGACY_PROTOCOL_V6_READER_SHA256, LEGACY_SINGLE_CALL_READER_SHA256},
        _include_workdir=manifest.get("schema_version") == 6
        or manifest.get("context_reader_sha256") in LEGACY_WORKDIR_READER_SHA256S,
    )

    if manifest.get("dispatch_protocol") != "paged-context-v8":
        return message
    # Preparation and validation share this rendering boundary; neither admits mutable cache contents.
    import review_prepare

    plan = _load_json(plan_path)
    entries = [entry for entry in plan["contexts"] if entry["role_id"] == role]
    if len(entries) != 1:
        raise SystemExit("review-context-role-count")
    return review_prepare._compact_dispatch_message(message, entries[0]["context_sha256"])


def _native_read_frame(
    manifest: dict[str, Any], plan_path: Path, role: str, attempt: dict[str, Any], page: int, canonical_call: str
) -> str:
    """Reconstruct compact setup or selectors from immutable identity while retaining historical frames."""
    if manifest.get("dispatch_protocol") != "paged-context-v8":
        return canonical_call
    import review_prepare

    first = render_read_call(
        _native_recipe_plan(manifest, plan_path),
        role,
        attempt["attempt"],
        manifest["context_reader_python"],
        reader_path=Path(manifest["context_reader_path"]),
    )
    arguments = review_prepare._read_arguments(canonical_call) if page == 1 else None
    return review_prepare._compact_read_call(first, attempt["context_sha256"], page, arguments=arguments)


def _reader_command_matches(observed: object, expected: str) -> bool:
    """Require a normalized command or one closed shell invocation with no additional executable arguments.

    Native host receipts use absolute POSIX shells; declared Windows coordinates remain strings rather than host-local
    paths. Unknown wrappers fail closed so a new provider form needs evidence before admission.
    """
    if observed == [expected]:
        return True
    if not isinstance(observed, list) or len(observed) != 3 or observed[2] != expected:
        return False
    executable, flag = observed[:2]
    if not isinstance(executable, str) or not isinstance(flag, str):
        return False
    if executable in {"/bin/bash", "/bin/zsh", "/bin/sh"}:
        return flag in {"-c", "-lc"}
    coordinate = executable.replace("\\", "/").casefold()
    if coordinate == "cmd.exe" or re.fullmatch(r"[a-z]:/windows/system32/cmd\.exe", coordinate):
        return flag.casefold() == "/c"
    if coordinate in {"powershell.exe", "pwsh.exe"} or re.fullmatch(
        r"[a-z]:/windows/system32/windowspowershell/v1\.0/powershell\.exe", coordinate
    ):
        return flag.casefold() == "-command"
    return False


def _literal_duplicated_plan_command(
    source: object, expected_call: str, *, windows: bool = False
) -> tuple[str, str] | None:
    """Prove one inert earlier plan literal by exact argv and shell serialization, never by stdout equivalence."""
    if not isinstance(source, str):
        return None
    import review_prepare

    try:
        actual = review_prepare._read_arguments(source)
        expected = review_prepare._read_arguments(expected_call)
        argv = shlex.split(actual["cmd"], posix=not windows)
        canonical = shlex.split(expected["cmd"], posix=not windows)
    except (KeyError, TypeError, ValueError):
        return None
    if windows:
        argv = [value[1:-1] if value.startswith('"') and value.endswith('"') else value for value in argv]
        canonical = [value[1:-1] if value.startswith('"') and value.endswith('"') else value for value in canonical]
    if len(argv) != len(canonical) + 2 or len(argv) < 6 or canonical[2] != "--plan":
        return None
    literal = argv[3]
    if (
        not (PurePosixPath(literal).is_absolute() or PureWindowsPath(literal).is_absolute())
        or re.fullmatch(r"[A-Za-z0-9_./:\\-]+", literal) is None
        or argv != [*canonical[:3], literal, "--plan", *canonical[3:]]
    ):
        return None
    serialized = subprocess.list2cmdline(argv) if windows else shlex.join(argv)
    if serialized != actual["cmd"] or actual != {**expected, "cmd": serialized}:
        return None
    candidate = (
        '// @exec: {"max_output_tokens": 10000}\n'
        + f"const r = await tools.exec_command({json.dumps(actual, ensure_ascii=False)}); text(r.output);"
    )
    return candidate, serialized


def _native_recipe_plan(manifest: dict[str, Any], retained_plan: Path) -> Path:
    """Preserve historical call coordinates only when their plan bytes match the retained plan."""
    original = manifest.get("original_plan_path")
    if original is None:
        return retained_plan
    path = Path(str(original))
    if (
        manifest.get("schema_version") not in {7, 8}
        or manifest.get("dispatch_protocol") != "paged-context-v6"
        or not path.is_absolute()
        or not path.is_file()
        or path.name != "inspection-plan.json"
        or path.read_bytes() != retained_plan.read_bytes()
    ):
        raise SystemExit("manifest-original-plan-identity-invalid")
    return path


def _manifest_passes(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    passes = manifest.get("passes", manifest.get("specialist_passes"))
    if not isinstance(passes, list):
        raise SystemExit("manifest-missing-passes")
    normalized = []
    for index, item in enumerate(passes):
        if not isinstance(item, dict):
            raise SystemExit(f"manifest-pass-not-object:{index}")
        normalized.append(item)
    return normalized


def _validate_inspection_plan(
    out_dir: Path, manifest: dict[str, Any], parent_thread_id: str
) -> tuple[dict[str, Any], dict[str, Path], bool, str | None]:
    """Bind schema-five inspection evidence to its exact no-execution plan."""
    execution = manifest.get("inspection_execution")
    if not isinstance(execution, dict) or set(execution) != {"plan_path", "plan_sha256"}:
        raise SystemExit("review-inspection-plan-missing")
    plan_path = _resolve_path(out_dir, execution["plan_path"])
    if not plan_path.is_file() or _sha256(plan_path) != execution["plan_sha256"]:
        raise SystemExit("review-inspection-plan-hash-mismatch")
    plan = _load_json(plan_path)
    required_keys = {
        "consumer_policy",
        "review_operation",
        "write_policy",
        "review_run_id",
        "parent_thread_id",
        "review_input_sha256",
        "source_sensitivity",
        "contexts",
        "independent_review_required",
        "independence_requirement_evidence",
    }
    if "review_topology" in plan:
        if plan["review_topology"] != "source-only":
            raise SystemExit("review-inspection-plan-topology-invalid")
        required_keys.add("review_topology")
    if set(plan) != required_keys:
        raise SystemExit("review-inspection-plan-shape-invalid")
    policy = plan["consumer_policy"]
    consumer = policy.get("consumer_id") if isinstance(policy, dict) else None
    if consumer not in {"code-review", "challenge-resolve"} or policy != {
        "consumer_id": consumer,
        "capability": "instruction-bounded-review",
        "promotion_status": "promoted",
        "parent_mutations": "serial",
        "canonical_gates": "serial",
    }:
        raise SystemExit("review-inspection-plan-policy-invalid")
    if consumer == "challenge-resolve" and (
        not isinstance(plan["contexts"], list)
        or len(plan["contexts"]) != 1
        or not isinstance(plan["contexts"][0], dict)
        or plan["contexts"][0].get("role_id") != "challenger"
    ):
        raise SystemExit("review-inspection-plan-challenge-role-invalid")
    if plan["review_operation"] != "inspection-only" or plan["write_policy"] != {
        "parent_writes": "none",
        "approval_requirement": "not-required",
    }:
        raise SystemExit("review-inspection-plan-operation-invalid")
    if plan["source_sensitivity"] != "non-sensitive":
        raise SystemExit("review-inspection-plan-source-sensitivity-invalid")
    for key in ("review_run_id", "parent_thread_id", "review_input_sha256"):
        if plan[key] != manifest.get(key):
            raise SystemExit(f"review-inspection-plan-identity-mismatch:{key}")
    if plan["parent_thread_id"] != parent_thread_id:
        raise SystemExit("review-inspection-plan-parent-thread-mismatch")
    independent_required = plan["independent_review_required"]
    evidence = plan["independence_requirement_evidence"]
    if type(independent_required) is not bool:
        raise SystemExit("review-inspection-plan-independence-required-invalid")
    if independent_required:
        if not isinstance(evidence, str) or not evidence.strip():
            raise SystemExit("review-inspection-plan-independence-evidence-missing")
    elif evidence is not None:
        raise SystemExit("review-inspection-plan-independence-evidence-unexpected")
    try:
        contexts = validate_inspection_contexts(
            plan,
            plan_path,
            context_limit=None
            if manifest.get("schema_version") in {7, 8} and manifest.get("manifest_kind") == "native-wave"
            else 4,
        )
    except ValueError as error:
        raise SystemExit(f"review-inspection-contexts-invalid:{error}") from error
    return plan, contexts, independent_required, evidence


def _child_controls(child_rows: list[dict[str, Any]], turn_id: str) -> dict[str, str]:
    """Report observed child controls without treating them as an isolation guarantee."""
    contexts = [
        row["payload"]
        for row in child_rows
        if row.get("type") == "turn_context"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("turn_id") == turn_id
    ]
    if len(contexts) != 1:
        raise SystemExit("review-inspection-turn-context-missing")
    context = contexts[0]
    sandbox = context.get("sandbox_mode")
    if sandbox is None and isinstance(context.get("sandbox_policy"), dict):
        sandbox = context["sandbox_policy"].get("type")
    approval = context.get("approval_policy")
    return {
        "sandbox_mode": sandbox if isinstance(sandbox, str) and sandbox else "unknown",
        "approval_policy": approval if isinstance(approval, str) and approval else "unknown",
    }


def _inspection_child_called_tool(child_rows: list[dict[str, Any]]) -> bool:
    """Reject every child tool-call or tool-result record for supplied-context inspection."""
    for row in child_rows:
        payload = row.get("payload")
        if row.get("type") == "response_item" and isinstance(payload, dict):
            if payload.get("type") not in {"agent_message", "message", "reasoning"}:
                return True
        elif row.get("type") == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "item_completed":
                item = payload.get("item")
                if not isinstance(item, dict) or item.get("type") not in {
                    "AgentMessage",
                    "Reasoning",
                    "ContextCompaction",
                }:
                    return True
            elif payload.get("type") not in {"task_started", "task_complete", "token_count", "thread_settings_applied"}:
                return True
    return False


def _missing_reader_error_path(diagnostic: object) -> str | None:
    """Recognize Python's pre-source missing-script error without converting its serialized filename."""
    missing = (
        re.fullmatch(r"(.+?): can't open file '(.+)': \[Errno 2\] No such file or directory\r?\n?", diagnostic)
        if isinstance(diagnostic, str)
        else None
    )
    if missing is None or not (
        (PurePosixPath(missing[1]).is_absolute() or PureWindowsPath(missing[1]).is_absolute())
        and (
            PurePosixPath(missing[1]).name.lower().startswith("python")
            or PureWindowsPath(missing[1]).name.lower().startswith("python")
        )
    ):
        return None
    # Python quotes the filename with repr(), which doubles every backslash of a Windows coordinate.
    return missing[2].replace("\\\\", "\\")


def _binary_source_diagnostic(diagnostic: object, executable: str) -> bool:
    """Recognize the error Python prints when its own interpreter binary is run as a script.

    The exact text depends on the interpreter version and the binary format: newer versions print a traceback frame
    naming the executable followed by the offending source line, older ones print a bare message, and an ELF, Mach-O
    or PE file fails differently on each. Python 3.10 checks UTF-8 validity only up to the first NUL byte of the first
    two lines, so the same version prints a Non-UTF-8 message for one binary and a framed ``invalid syntax`` for
    another; each (version, binary format) pair a CI matrix runs therefore needs a row in the receipt tests. Only a
    complete receipt that names this executable is accepted, so unrelated output or a SyntaxError from some other
    file never proves a duplicated interpreter token.

    Examples:
        >>> _binary_source_diagnostic("SyntaxError: source code cannot contain null bytes\\n", "/usr/bin/python3")
        False
        >>> frame = '  File "/usr/bin/python3", line 1\\n    x\\n'
        >>> message = "SyntaxError: source code cannot contain null bytes\\n"
        >>> _binary_source_diagnostic(frame + message, "/usr/bin/python3")
        True
    """
    if not isinstance(diagnostic, str):
        return False
    path = re.escape(executable)
    frame_head = r'  File "' + path + r'", line '
    frame = frame_head + r"\d+\r?\n(?:[^\r\n]*\r?\n){0,3}"
    frame_line_one = frame_head + r"1\r?\n"
    # The null-byte message names no file, so only the frame binds it to this executable.
    null_bytes = frame + r"SyntaxError: source code (?:string )?cannot contain null bytes"
    # The Non-UTF-8 message carries the path itself, so the frame is optional (older interpreters omit it).
    non_utf8 = (
        rf"(?:{frame})?SyntaxError: Non-UTF-8 code starting with '[^'\r\n]+' in file "
        + path
        + r" on line \d+, but no encoding declared; see "
        r"https://(?:peps\.python\.org|python\.org/dev/peps)/pep-0263/ for details"
    )
    # Python 3.10 reaches the tokenizer on an ELF binary whose first two lines are valid UTF-8 before the first NUL; the
    # frame, line 1, ELF magic echo and caret keep the generic message bound to this executable.
    invalid_syntax = frame_line_one + r"    \x7fELF[^\r\n]*\r?\n *\^\r?\nSyntaxError: invalid syntax"
    return re.fullmatch(rf"(?:{null_bytes}|{non_utf8}|{invalid_syntax})\r?\n?", diagnostic) is not None


def _validate_context_read(
    child_rows: list[dict[str, Any]],
    plan_path: Path,
    role: str,
    attempt: dict[str, Any],
    manifest: dict[str, Any],
    context: str,
    *,
    incomplete_dispatch: bool = False,
) -> None:
    """Require complete reads, one proved preprocessing correction, or a bounded launch error for reassessment."""
    # Resolve the existing producer/validator circular boundary only after both modules initialize.
    import review_prepare

    tool_rows = [
        row["payload"]
        for row in child_rows
        if row.get("type") == "response_item"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type")
        in {"custom_tool_call", "function_call", "custom_tool_call_output", "function_call_output"}
    ]
    commands = [
        row["payload"]["item"]
        for row in child_rows
        if row.get("type") == "event_msg"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type") == "item_completed"
        and isinstance(row["payload"].get("item"), dict)
        and row["payload"]["item"].get("type") == "CommandExecution"
    ]
    page_count = len(context_pages(context))
    optional_pragma = (
        manifest.get("schema_version") == 8
        and manifest.get("context_reader_sha256") == _sha256(SKILL_DIRECTORY / "review_context.py")
        and len(commands) == len(tool_rows) // 2
    )

    def accepted_call_inputs(expected_call: str) -> set[str]:
        """Admit only exact reader code with the equivalent optional outer output-budget line."""
        accepted = {expected_call, expected_call + "\n"}
        if optional_pragma:
            # The exact pragma and functions.exec's default both budget 10000 output tokens.
            # Failed plan loads and canonical retries share this rule; inner commands and receipts stay exact.
            unadorned = expected_call.removeprefix('// @exec: {"max_output_tokens": 10000}\n')
            accepted.update({unadorned, unadorned + "\n"})
        return accepted

    retained_tool_rows, retained_commands = tool_rows, commands
    # The host can reject malformed JSON before executing the exact reader body. Admit one immediate
    # correction only with that preprocessing receipt and every canonical command still accounted for.
    if (
        not incomplete_dispatch
        and manifest.get("schema_version") == 8
        and manifest.get("context_reader_sha256") == _sha256(SKILL_DIRECTORY / "review_context.py")
        and len(tool_rows) == 2 * (page_count + 1)
        and len(commands) == page_count
    ):
        rejected = [index for index in range(page_count + 1) if isinstance(tool_rows[2 * index + 1].get("output"), str)]
        if len(rejected) != 1 or rejected[0] >= page_count:
            raise SystemExit(f"review-inspection-context-read-retry-invalid:{role}")
        index = rejected[0]
        call, output = tool_rows[2 * index : 2 * index + 2]
        expected_call = render_read_call(
            _native_recipe_plan(manifest, plan_path),
            role,
            attempt["attempt"],
            manifest["context_reader_python"],
            index + 1,
            reader_path=Path(manifest["context_reader_path"]),
        )
        expected_call = _native_read_frame(manifest, plan_path, role, attempt, index + 1, expected_call)
        malformed = call.get("input")
        invalid_json = False
        body = ""
        if isinstance(malformed, str) and malformed.startswith("// @exec: ") and "\n" in malformed:
            pragma, body = malformed.split("\n", 1)
            try:
                json.loads(pragma.removeprefix("// @exec: "))
            except json.JSONDecodeError:
                invalid_json = True
        optional_pragma = True
        if (
            not invalid_json
            or body not in {expected_call.split("\n", 1)[1], expected_call.split("\n", 1)[1] + "\n"}
            or call.get("type") != "custom_tool_call"
            or call.get("name") != "exec"
            or not isinstance(call.get("call_id"), str)
            or output.get("type") != "custom_tool_call_output"
            or output.get("call_id") != call["call_id"]
            or not re.fullmatch(
                r"exec pragma must be valid JSON with supported fields `yield_time_ms` and `max_output_tokens`: "
                r"[^\r\n]+ at line [1-9][0-9]* column [0-9]+",
                output["output"],
            )
            or tool_rows[2 * index + 2].get("name") != "exec"
            or tool_rows[2 * index + 2].get("input") not in accepted_call_inputs(expected_call)
        ):
            raise SystemExit(f"review-inspection-context-read-retry-invalid:{role}")
        tool_rows = tool_rows[: 2 * index] + tool_rows[2 * index + 2 :]
    # A proved missing reader or plan fails before source access. Keep its evidence while requiring the next call
    # to retry that same canonical page; one incidental launch never substitutes for source coverage.
    if (
        not incomplete_dispatch
        and manifest.get("schema_version") == 8
        and manifest.get("context_reader_sha256") == _sha256(SKILL_DIRECTORY / "review_context.py")
        and len(tool_rows) == 2 * (page_count + 1)
        and len(commands) == page_count + 1
    ):
        failed = [index for index, command in enumerate(commands) if command.get("exit_code") != 0]
        if len(failed) != 1 or failed[0] >= page_count:
            raise SystemExit(f"review-inspection-context-read-retry-invalid:{role}")
        index = failed[0]
        call, output = tool_rows[2 * index : 2 * index + 2]
        try:
            arguments = review_prepare._read_arguments(call["input"])
            argv = shlex.split(arguments["cmd"], posix=os.name != "nt")
            reader_coordinate = argv[1].strip('"') if os.name == "nt" else argv[1]
            missing_reader = Path(reader_coordinate) != Path(manifest["context_reader_path"]).resolve()
            coordinate = reader_coordinate if missing_reader else argv[argv.index("--plan") + 1]
            missing_path = Path(coordinate.strip('"') if os.name == "nt" else coordinate)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise SystemExit(f"review-inspection-context-read-retry-invalid:{role}") from error
        failed_call = render_read_call(
            _native_recipe_plan(manifest, plan_path) if missing_reader else missing_path,
            role,
            attempt["attempt"],
            manifest["context_reader_python"],
            index + 1,
            reader_path=missing_path if missing_reader else Path(manifest["context_reader_path"]),
        )
        next_call = render_read_call(
            _native_recipe_plan(manifest, plan_path),
            role,
            attempt["attempt"],
            manifest["context_reader_python"],
            index + 1,
            reader_path=Path(manifest["context_reader_path"]),
        )
        if manifest.get("dispatch_protocol") == "paged-context-v8" and index != 0:
            raise SystemExit(f"review-inspection-context-read-retry-invalid:{role}")
        failed_call = _native_read_frame(manifest, plan_path, role, attempt, index + 1, failed_call)
        next_call = _native_read_frame(manifest, plan_path, role, attempt, index + 1, next_call)
        receipt = output.get("output")
        command = commands[index]
        diagnostic = (
            receipt[1].get("text")
            if isinstance(receipt, list) and len(receipt) == 2 and isinstance(receipt[1], dict)
            else None
        )
        proved_diagnostic = isinstance(diagnostic, str) and (
            _missing_reader_error_path(diagnostic) == str(missing_path)
            if missing_reader
            else diagnostic
            in {
                "review-context-read-failed:"
                + str(FileNotFoundError(2, "No such file or directory", str(missing_path)))
                + newline
                for newline in ("\n", "\r\n")
            }
        )
        canonical_path = Path(manifest["context_reader_path"]) if missing_reader else plan_path
        if (
            not missing_path.is_absolute()
            or missing_path.name != canonical_path.name
            or missing_path.exists()
            or missing_path.resolve() == canonical_path.resolve()
            or call.get("name") != "exec"
            or call.get("input") not in accepted_call_inputs(failed_call)
            or tool_rows[2 * index + 2].get("name") != "exec"
            or tool_rows[2 * index + 2].get("input") not in accepted_call_inputs(next_call)
            or not isinstance(call.get("call_id"), str)
            or output.get("call_id") != call["call_id"]
            or not isinstance(receipt, list)
            or len(receipt) != 2
            or not isinstance(receipt[0], dict)
            or receipt[0].get("type") != "input_text"
            or not isinstance(receipt[0].get("text"), str)
            or not receipt[0]["text"].startswith("Script completed\n")
            or not isinstance(receipt[1], dict)
            or receipt[1].get("type") != "input_text"
            or not proved_diagnostic
            or not _reader_command_matches(command.get("command"), arguments["cmd"])
            or command.get("exit_code") != (2 if missing_reader else 1)
            or (command.get("stdout"), command.get("stderr"))
            not in {(receipt[1]["text"], ""), ("", receipt[1]["text"])}
        ):
            raise SystemExit(f"review-inspection-context-read-retry-invalid:{role}")
        tool_rows = tool_rows[: 2 * index] + tool_rows[2 * index + 2 :]
        commands = commands[:index] + commands[index + 1 :]
    if (not incomplete_dispatch and len(tool_rows) != 2 * page_count) or (
        incomplete_dispatch and (len(tool_rows) % 2 or not 4 <= len(tool_rows) <= 2 * page_count)
    ):
        raise SystemExit(f"review-inspection-context-read-count-mismatch:{role}")
    expected_commands: list[str] = []
    expected_outputs: list[str] = []
    expected_exits: list[int] = []
    failure_kind: str | None = None
    successful = 0
    literal_plan_page = None
    if manifest.get("dispatch_protocol") == "paged-context-v8" and not commands:
        raise SystemExit(f"review-inspection-context-command-mismatch:{role}")
    for page in range(1, len(tool_rows) // 2 + 1):
        call, output = tool_rows[2 * (page - 1) : 2 * page]
        expected_call = render_read_call(
            _native_recipe_plan(manifest, plan_path),
            role,
            attempt["attempt"],
            manifest["context_reader_python"],
            page,
            reader_path=Path(manifest["context_reader_path"]) if manifest.get("schema_version") in {7, 8} else None,
            _include_workdir=manifest.get("schema_version") == 6
            or manifest.get("context_reader_sha256") in LEGACY_WORKDIR_READER_SHA256S,
        )
        expected_command = json.loads(expected_call.split("tools.exec_command(", 1)[1].split("); text", 1)[0])["cmd"]
        expected_call = _native_read_frame(manifest, plan_path, role, attempt, page, expected_call)
        expected_output = render_read_output(
            context,
            role,
            manifest["review_run_id"],
            manifest["review_input_sha256"],
            attempt["context_sha256"],
            attempt["attempt"],
            page,
        )
        canonical_call = call.get("name") == "exec" and call.get("input") in accepted_call_inputs(expected_call)
        if (
            not canonical_call
            and not incomplete_dispatch
            and literal_plan_page is None
            and manifest.get("schema_version") == 8
            and manifest.get("dispatch_protocol") == "paged-context-v7"
            and manifest.get("context_reader_sha256")
            == "ba6ef524abf56f597e6f88f5d63ac1429995c066c75afc81551803edbae01d2e"
            and len(retained_tool_rows) == 2 * page_count
            and len(retained_commands) == page_count
            and call.get("name") == "exec"
        ):
            proved = _literal_duplicated_plan_command(call.get("input"), expected_call, windows=os.name == "nt")
            if proved is not None and call.get("input") in accepted_call_inputs(proved[0]):
                candidate_command = commands[page - 1]
                # Retain the actual audited command; only observed single-command wrappers qualify.
                if (
                    _reader_command_matches(candidate_command.get("command"), proved[1])
                    and candidate_command.get("stderr") == ""
                ):
                    canonical_call = True
                    expected_command = proved[1]
                    literal_plan_page = page
        if output.get("call_id") != call.get("call_id") or not isinstance(call.get("call_id"), str):
            raise SystemExit(f"review-inspection-context-read-receipt-mismatch:{role}:{page}")
        receipt = output.get("output")
        valid_receipt = not (
            not isinstance(receipt, list)
            or len(receipt) != 2
            or not isinstance(receipt[0], dict)
            or receipt[0].get("type") != "input_text"
            or not isinstance(receipt[0].get("text"), str)
            or not receipt[0]["text"].startswith("Script completed\n")
        )
        canonical_output = valid_receipt and receipt[1] == {"type": "input_text", "text": expected_output}
        exit_code = 0
        if incomplete_dispatch and not (canonical_call and canonical_output):
            diagnostic = receipt[1].get("text") if valid_receipt and isinstance(receipt[1], dict) else None
            # Only one extra copy of the exact interpreter token explains this binary-as-source failure.
            arguments = json.loads(expected_call.split("tools.exec_command(", 1)[1].split("); text", 1)[0])
            executable = manifest["context_reader_python"]
            quoted_executable = subprocess.list2cmdline([executable]) if os.name == "nt" else shlex.quote(executable)
            duplicate_arguments = {**arguments, "cmd": quoted_executable + " " + arguments["cmd"]}
            duplicate_call = (
                '// @exec: {"max_output_tokens": 10000}\n'
                f"const r = await tools.exec_command({json.dumps(duplicate_arguments, ensure_ascii=False)}); text(r.output);"
            )
            if call.get("name") == "exec" and call.get("input") in {duplicate_call, duplicate_call + "\n"}:
                binary_diagnostic = _binary_source_diagnostic(diagnostic, executable)
                if (
                    manifest.get("schema_version") not in {7, 8}
                    or not binary_diagnostic
                    or receipt[1].get("type") != "input_text"
                    or failure_kind is not None
                    or len(tool_rows) != 2 * page_count
                ):
                    raise SystemExit(f"review-repair-dispatch-cause-unproven:{role}:{page}")
                failure_kind = "duplicate-interpreter"
                expected_commands.append(duplicate_arguments["cmd"])
                expected_outputs.append(diagnostic)
                expected_exits.append(1)
                continue
            missing_coordinate = _missing_reader_error_path(diagnostic)
            missing_path = Path(missing_coordinate) if missing_coordinate is not None else None
            if (
                failure_kind == "duplicate-interpreter"
                or missing_path is None
                or not missing_path.is_absolute()
                or missing_path.name != "review_context.py"
                or missing_path.exists()
                or missing_path == Path(manifest["context_reader_path"])
            ):
                raise SystemExit(f"review-repair-dispatch-cause-unproven:{role}:{page}")
            malformed = render_read_call(
                _native_recipe_plan(manifest, plan_path),
                role,
                attempt["attempt"],
                manifest["context_reader_python"],
                page,
                missing_path,
                _include_workdir=manifest.get("schema_version") == 6
                or manifest.get("context_reader_sha256") in LEGACY_WORKDIR_READER_SHA256S,
            )
            if call.get("name") != "exec" or call.get("input") not in {malformed, malformed + "\n"}:
                raise SystemExit(f"review-repair-dispatch-cause-unproven:{role}:{page}")
            failure_kind = "missing-reader"
            expected_command = json.loads(malformed.split("tools.exec_command(", 1)[1].split("); text", 1)[0])["cmd"]
            expected_output = diagnostic
            exit_code = 2
        elif not canonical_call:
            raise SystemExit(f"review-inspection-context-read-call-mismatch:{role}:{page}")
        elif not canonical_output:
            raise SystemExit(f"review-inspection-context-read-output-mismatch:{role}:{page}")
        elif failure_kind == "missing-reader":
            raise SystemExit(f"review-repair-dispatch-read-after-failure:{role}:{page}")
        else:
            successful += 1
        expected_commands.append(expected_command)
        expected_outputs.append(expected_output)
        expected_exits.append(exit_code)
    if incomplete_dispatch and (not successful or failure_kind is None):
        raise SystemExit(f"review-repair-dispatch-cause-unproven:{role}")
    if failure_kind == "duplicate-interpreter" and not commands:
        raise SystemExit(f"review-repair-dispatch-cause-unproven:{role}")
    if commands:
        if len(commands) != len(expected_commands):
            raise SystemExit(f"review-inspection-context-command-mismatch:{role}")
        for page, (command, expected_command, expected_output, exit_code) in enumerate(
            zip(commands, expected_commands, expected_outputs, expected_exits), start=1
        ):
            if (
                not _reader_command_matches(command.get("command"), expected_command)
                or command.get("exit_code") != exit_code
                or (exit_code == 0 and command.get("stdout") != expected_output)
                or (
                    exit_code in {1, 2}
                    and (command.get("stdout"), command.get("stderr"))
                    not in {(expected_output, ""), ("", expected_output)}
                )
            ):
                raise SystemExit(f"review-inspection-context-command-mismatch:{role}:{page}")
    remaining = [
        row
        for row in child_rows
        if not (row.get("type") == "response_item" and row.get("payload") in retained_tool_rows)
        and not (
            row.get("type") == "event_msg"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("type") == "item_completed"
            and row["payload"].get("item") in retained_commands
        )
    ]
    if _inspection_child_called_tool(remaining):
        raise SystemExit(f"review-inspection-child-tool-use:{role}")


def _joined_terminal_timestamp(parent_rows: list[dict[str, Any]], agent_path: str, message: str) -> datetime | None:
    """Return the unique parent-visible final-answer join matching one child output."""
    joined: list[datetime] = []
    for row in parent_rows:
        payload = row.get("payload")
        if row.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("type") != "agent_message" or payload.get("author") != agent_path:
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        texts = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "input_text"]
        if (
            len(texts) == 1
            and isinstance(texts[0], str)
            and texts[0].startswith("Message Type: FINAL_ANSWER\n")
            and texts[0].split("Payload:\n", 1)[-1].strip() == message
        ):
            try:
                timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError, AttributeError):
                return None
            if timestamp.tzinfo is None:
                return None
            joined.append(timestamp)
    return joined[0] if len(joined) == 1 else None


def _joined_terminal_result(parent_rows: list[dict[str, Any]], agent_path: str, message: str) -> bool:
    """Require one parent-visible child final message matching the bound output exactly."""
    joined = 0
    for row in parent_rows:
        payload = row.get("payload")
        if row.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("type") != "agent_message" or payload.get("author") != agent_path:
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        texts = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "input_text"]
        if (
            len(texts) == 1
            and isinstance(texts[0], str)
            and texts[0].startswith("Message Type: FINAL_ANSWER\n")
            and texts[0].split("Payload:\n", 1)[-1].strip() == message
        ):
            joined += 1
    return joined == 1


def _parent_timestamp(row: dict[str, Any]) -> datetime:
    """Read one timezone-aware parent event timestamp or reject its evidence."""
    try:
        value = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise SystemExit("review-inspection-schedule-timestamp-invalid") from error
    if value.tzinfo is None:
        raise SystemExit("review-inspection-schedule-timestamp-invalid")
    return value


def _related_capacity_release(
    codex_home: Path,
    parent_thread_id: str,
    parent_rows: list[dict[str, Any]],
    refusal: dict[str, Any],
    next_call_at: datetime,
) -> tuple[str, str] | None:
    """Prove one active sibling turn released a shared immediate-parent slot and notified a blocking wait."""
    session = parent_rows[0].get("payload", {}) if parent_rows and parent_rows[0].get("type") == "session_meta" else {}
    spawn = (
        session.get("source", {}).get("subagent", {}).get("thread_spawn", {})
        if isinstance(session.get("source"), dict)
        else {}
    )
    ancestor_id, operator_path = spawn.get("parent_thread_id"), spawn.get("agent_path")
    if (
        session.get("id") != parent_thread_id
        or not isinstance(ancestor_id, str)
        or ancestor_id == parent_thread_id
        or not isinstance(operator_path, str)
        or session.get("parent_thread_id") != ancestor_id
        or session.get("agent_path") != operator_path
    ):
        return None
    ancestor = _read_jsonl(_find_rollout(codex_home, ancestor_id))
    if (
        not ancestor
        or ancestor[0].get("type") != "session_meta"
        or ancestor[0].get("payload", {}).get("id") != ancestor_id
    ):
        return None
    ancestor_path = operator_path.rsplit("/", 1)[0]
    notifications = []
    for row in ancestor:
        payload = row.get("payload", {})
        if (
            row.get("type") != "response_item"
            or payload.get("type") != "function_call"
            or payload.get("name") != "send_message"
        ):
            continue
        try:
            args = json.loads(payload.get("arguments", ""))
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(args, dict)
            or set(args) != {"target", "message"}
            or args["target"] not in {operator_path, operator_path.rsplit("/", 1)[-1]}
            or not isinstance(args["message"], str)
            or not args["message"]
        ):
            continue
        sent = _parent_timestamp(row)
        activities = [
            event
            for event in _event_payloads(ancestor, "sub_agent_activity")
            if event.get("event_id") == payload.get("call_id")
            and event.get("kind") == "interacted"
            and event.get("agent_thread_id") == parent_thread_id
            and event.get("agent_path") == operator_path
        ]
        if len(activities) != 1:
            continue
        activity_start = activities[0].get("started_at_ms")
        activity_end = activities[0].get("completed_at_ms")
        if (
            any(type(value) not in {int, float} or not math.isfinite(value) for value in (activity_start, activity_end))
            or not sent.timestamp() * 1000 <= activity_start <= activity_end < next_call_at.timestamp() * 1000
        ):
            continue
        receipts = [
            r
            for r in ancestor
            if r.get("type") == "response_item"
            and r.get("payload", {}).get("type") == "function_call_output"
            and r["payload"].get("call_id") == payload.get("call_id")
        ]
        if (
            len(receipts) != 1
            or receipts[0]["payload"].get("output") != ""
            or not sent < _parent_timestamp(receipts[0]) < next_call_at
        ):
            continue
        delivered = [
            r
            for r in parent_rows
            if r.get("type") == "response_item"
            and r.get("payload", {}).get("type") == "agent_message"
            and r["payload"].get("author") == ancestor_path
            and r["payload"].get("recipient") == operator_path
            and r["payload"].get("content")
            == [
                {
                    "type": "input_text",
                    "text": f"Message Type: MESSAGE\nTask name: {operator_path}\nSender: {ancestor_path}\nPayload:\n",
                },
                {"type": "encrypted_content", "encrypted_content": args["message"]},
            ]
        ]
        if len(delivered) != 1:
            continue
        received = _parent_timestamp(delivered[0])
        if not refusal["returned_at"] < sent <= received < next_call_at:
            continue
        for wait in parent_rows:
            wp = wait.get("payload", {})
            if (
                wait.get("type") != "response_item"
                or wp.get("type") != "function_call"
                or wp.get("name") != "wait_agent"
            ):
                continue
            outputs = [
                r
                for r in parent_rows
                if r.get("type") == "response_item"
                and r.get("payload", {}).get("type") == "function_call_output"
                and r["payload"].get("call_id") == wp.get("call_id")
            ]
            if len(outputs) != 1:
                continue
            try:
                result = json.loads(outputs[0]["payload"].get("output", ""))
            except (TypeError, json.JSONDecodeError):
                continue
            started, ended = _parent_timestamp(wait), _parent_timestamp(outputs[0])
            if (
                result == {"message": "Wait completed.", "timed_out": False}
                and refusal["returned_at"] < started <= sent <= ended <= received < next_call_at
            ):
                notifications.append(sent)
    if not notifications:
        return None
    releases = []
    for path in (codex_home / "sessions").rglob("*.jsonl"):
        try:
            with path.open(encoding="utf-8") as stream:
                meta = json.loads(stream.readline())
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        ss = meta.get("payload", {}) if meta.get("type") == "session_meta" else {}
        source = ss.get("source")
        sp = source.get("subagent", {}).get("thread_spawn", {}) if isinstance(source, dict) else {}
        if sp.get("parent_thread_id") != ancestor_id or ss.get("id") == parent_thread_id:
            continue
        agent_path = sp.get("agent_path")
        if (
            ss.get("parent_thread_id") != ancestor_id
            or ss.get("agent_path") != agent_path
            or not isinstance(agent_path, str)
            or agent_path.rsplit("/", 1)[0] != ancestor_path
        ):
            continue
        rows = _read_jsonl(_find_rollout(codex_home, ss["id"]))
        for terminal_row in rows:
            terminal = terminal_row.get("payload", {})
            if terminal_row.get("type") != "event_msg" or terminal.get("type") != "task_complete":
                continue
            turn = terminal.get("turn_id")
            starts = [
                r
                for r in rows
                if r.get("type") == "event_msg"
                and r.get("payload", {}).get("type") == "task_started"
                and r["payload"].get("turn_id") == turn
            ]
            ends = [
                r
                for r in rows
                if r.get("type") == "event_msg"
                and r.get("payload", {}).get("type") == "task_complete"
                and r["payload"].get("turn_id") == turn
            ]
            message = terminal.get("last_agent_message")
            if len(starts) != 1 or len(ends) != 1 or not isinstance(turn, str) or not isinstance(message, str):
                continue
            started, ended = _parent_timestamp(starts[0]), _parent_timestamp(terminal_row)
            joined = _joined_terminal_timestamp(ancestor, agent_path, message.strip())
            if (
                joined is not None
                and started <= refusal["called_at"] < refusal["returned_at"] < ended <= joined
                and any(joined < sent for sent in notifications)
            ):
                releases.append((ss["id"], turn))
    return releases[0] if len(releases) == 1 else None


def _validate_native_schedule(
    out_dir: Path,
    manifest: dict[str, Any],
    passes: list[dict[str, Any]],
    parent_rows: list[dict[str, Any]],
    frozen_contexts: dict[str, Path],
    codex_home: Path,
    parent_thread_id: str,
    roles_dir: Path,
) -> bool:
    """Bind native dispatch order, original and correction allocations, refusals, and waits to parent evidence."""
    plan_path = _resolve_path(out_dir, manifest["inspection_execution"]["plan_path"])
    ordered_roles = sorted(frozen_contexts, key=lambda role: (-len(frozen_contexts[role].read_bytes()), role))
    role_attempts: dict[str, list[dict[str, Any]]] = {}
    for item in passes:
        role_attempts[item["role"]] = item["attempts"]

    allocations: list[dict[str, Any]] = []
    successful_call_ids: set[str] = set()
    expected_task_names: dict[str, tuple[str, int]] = {}
    expected_task_arguments: dict[str, dict[str, Any]] = {}
    for role in ordered_roles:
        card = _load_role_card(roles_dir, role)
        for attempt in role_attempts[role]:
            number = attempt["attempt"]
            call_id = attempt.get("spawn_call_id")
            records = [
                row
                for row in parent_rows
                if row.get("type") == "response_item"
                and isinstance(row.get("payload"), dict)
                and row["payload"].get("call_id") == call_id
            ]
            calls = [row for row in records if row["payload"].get("type") == "function_call"]
            outputs = [row for row in records if row["payload"].get("type") == "function_call_output"]
            if len(calls) != 1 or len(outputs) != 1 or calls[0]["payload"].get("name") != "spawn_agent":
                raise SystemExit(f"review-inspection-launch-receipt-invalid:{role}:{number}")
            try:
                arguments = json.loads(calls[0]["payload"].get("arguments", ""))
                receipt = json.loads(outputs[0]["payload"].get("output", ""))
            except (TypeError, json.JSONDecodeError) as error:
                raise SystemExit(f"review-inspection-launch-receipt-invalid:{role}:{number}") from error
            expected = {
                "task_name": f"review_{role.replace('-', '_')}_{attempt['context_sha256'][:12]}_a{number}",
                "agent_type": "default",
                "fork_turns": "none",
                "model": card["model"],
                "reasoning_effort": card["model_reasoning_effort"],
                "message": _native_dispatch_message(manifest, plan_path, role, number),
            }
            recovered = next(item for item in passes if item["role"] == role)
            if recovered.get("recovery") is not None and number == 2:
                expected = _recovery_arguments(out_dir, manifest, recovered, parent_rows, codex_home)
                expected["agent_type"] = "default"
            elif recovered.get("recovery") == {"kind": "incomplete-dispatch"}:
                expected["message"] = _original_dispatch_message(manifest, plan_path, role, attempt, parent_rows)
            # Refusals allocate the same task as successful spawns, including any proved recovery instruction.
            # Retain canonical controls before the host's optional agent selector normalization.
            expected_task_arguments[expected["task_name"]] = dict(expected)
            # Some native hosts omit the unsupported selector; explicit nondefault values remain invalid.
            if isinstance(arguments, dict) and "agent_type" not in arguments:
                expected.pop("agent_type", None)
            arguments_match = arguments == expected
            if not arguments_match and _paged_native_manifest(manifest) and isinstance(arguments, dict):
                # Opaque host transport preserves exact child delivery; audited page reads bind the source separately.
                arguments_match = (
                    set(arguments) == set(expected)
                    and {key: value for key, value in arguments.items() if key != "message"}
                    == {key: value for key, value in expected.items() if key != "message"}
                    and _receipt_binds_child(
                        parent_rows,
                        codex_home,
                        parent_thread_id,
                        attempt,
                        expected["message"],
                        schema_version=6,
                        model=card["model"],
                        effort=card["model_reasoning_effort"],
                    )
                )
            if not arguments_match or receipt != {"task_name": attempt["agent_path"]}:
                raise SystemExit(f"review-inspection-launch-arguments-invalid:{role}:{number}")
            launched_at = _parent_timestamp(calls[0])
            received_at = _parent_timestamp(outputs[0])
            if launched_at >= received_at:
                raise SystemExit(f"review-inspection-launch-receipt-invalid:{role}:{number}")
            child_rows = _read_jsonl(_find_rollout(codex_home, attempt["agent_thread_id"]))
            terminals = [
                event
                for event in _event_payloads(child_rows, "task_complete")
                if event.get("turn_id") == attempt.get("turn_id")
            ]
            if len(terminals) != 1:
                raise SystemExit(f"review-inspection-attempt-timing-missing:{role}:{number}")
            terminal = terminals[0]
            terminal_start = terminal.get("started_at")
            terminal_end = terminal.get("completed_at")
            if (
                isinstance(terminal_start, bool)
                or isinstance(terminal_end, bool)
                or not isinstance(terminal_start, int | float)
                or not isinstance(terminal_end, int | float)
                or not math.isfinite(terminal_start)
                or not math.isfinite(terminal_end)
                or terminal_start >= terminal_end
            ):
                raise SystemExit(f"review-inspection-attempt-timing-invalid:{role}:{number}")
            terminal_at = datetime.fromtimestamp(terminal_end, timezone.utc)
            if attempt.get("status") == "completed":
                message = _resolve_path(out_dir, attempt["output_path"]).read_text(encoding="utf-8").strip()
            else:
                message = terminal.get("last_agent_message")
            if not isinstance(message, str):
                raise SystemExit(f"review-inspection-parent-join-missing:{role}")
            joined_at = _joined_terminal_timestamp(parent_rows, attempt["agent_path"], message.strip())
            # Integer host starts identify a whole-second bucket; precise child creation remains receipt-bound.
            start_predates_launch = (
                terminal_start + 1 <= launched_at.timestamp()
                if type(terminal_start) is int
                else terminal_start < launched_at.timestamp()
            )
            if start_predates_launch or joined_at is None or received_at > joined_at:
                raise SystemExit(f"review-inspection-parent-join-missing:{role}")
            successful_call_ids.add(call_id)
            expected_task_names[expected["task_name"]] = (role, number)
            allocations.append(
                {
                    "role": role,
                    "attempt": number,
                    "task_name": expected["task_name"],
                    "start": launched_at,
                    "launch_index": parent_rows.index(calls[0]),
                    "end": max(terminal_at, joined_at),
                }
            )

    for item in passes:
        if item.get("recovery") is None:
            continue
        retained = [allocation for allocation in allocations if allocation["role"] == item["role"]]
        if len(retained) != 2 or retained[1]["start"] <= retained[0]["end"]:
            raise SystemExit(f"review-repair-before-original-join:{item['role']}")

    first_launches: dict[str, tuple[datetime, int]] = {}
    for allocation in allocations:
        if allocation["attempt"] == 1:
            role = allocation["role"]
            first_launches[role] = (allocation["start"], allocation["launch_index"])
    if set(first_launches) != set(ordered_roles):
        raise SystemExit("review-inspection-context-role-set-mismatch")
    if [role for role, _ in sorted(first_launches.items(), key=lambda pair: pair[1][1])] != ordered_roles:
        raise SystemExit("review-inspection-dispatch-order-mismatch")

    capacity_refusals: list[dict[str, Any]] = []
    for row in parent_rows:
        payload = row.get("payload")
        if (
            row.get("type") != "response_item"
            or not isinstance(payload, dict)
            or payload.get("type") != "function_call"
            or payload.get("name") != "spawn_agent"
            or payload.get("call_id") in successful_call_ids
        ):
            continue
        try:
            arguments = json.loads(payload.get("arguments", ""))
        except (TypeError, json.JSONDecodeError):
            continue
        name = arguments.get("task_name") if isinstance(arguments, dict) else None
        if name not in expected_task_names:
            continue
        call_id = payload.get("call_id")
        matching = [
            candidate
            for candidate in parent_rows
            if candidate.get("type") == "response_item"
            and isinstance(candidate.get("payload"), dict)
            and candidate["payload"].get("type") == "function_call_output"
            and candidate["payload"].get("call_id") == call_id
        ]
        if (
            len(matching) != 1
            or matching[0]["payload"].get("output") != "collab spawn failed: agent thread limit reached"
        ):
            raise SystemExit(f"review-inspection-capacity-refusal-invalid:{expected_task_names[name][0]}")
        role = expected_task_names[name][0]
        expected_arguments = dict(expected_task_arguments[name])
        if "agent_type" not in arguments:
            expected_arguments.pop("agent_type")
        arguments_match = arguments == expected_arguments
        if (
            not arguments_match
            and manifest.get("schema_version") == 8
            and _paged_native_manifest(manifest)
            and manifest.get("context_reader_sha256") == _sha256(SKILL_DIRECTORY / "review_context.py")
            and isinstance(arguments.get("message"), str)
            and arguments["message"]
            and "```" not in arguments["message"]
        ):
            # A refused opaque message reaches no child. Keep exact controls, then prove refusal/no child
            # and a joined capacity release; the successful allocation separately binds delivery and source.
            arguments_match = set(arguments) == set(expected_arguments) and {
                key: value for key, value in arguments.items() if key != "message"
            } == {key: value for key, value in expected_arguments.items() if key != "message"}
        if not arguments_match:
            raise SystemExit(f"review-inspection-capacity-refusal-invalid:{role}")
        called_at = _parent_timestamp(row)
        returned_at = _parent_timestamp(matching[0])
        if called_at >= returned_at:
            raise SystemExit(f"review-inspection-capacity-refusal-invalid:{role}")
        for path in (codex_home / "sessions").rglob("*.jsonl"):
            try:
                with path.open(encoding="utf-8") as stream:
                    session_row = json.loads(stream.readline())
                session = session_row.get("payload", {})
                spawn = session.get("source", {}).get("subagent", {}).get("thread_spawn", {})
                session_path = session.get("agent_path") or spawn.get("agent_path", "")
            except (OSError, AttributeError, TypeError, ValueError, json.JSONDecodeError):
                continue
            session_name = str(session_path).replace("\\", "/").rsplit("/", 1)[-1]
            if spawn.get("parent_thread_id") != parent_thread_id or session_name != name:
                continue
            try:
                created_at = datetime.fromisoformat(session["timestamp"].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                raise SystemExit(f"review-inspection-capacity-refusal-invalid:{role}") from error
            if created_at.tzinfo is None:
                raise SystemExit(f"review-inspection-capacity-refusal-invalid:{role}")
            if called_at <= created_at <= returned_at:
                raise SystemExit(f"review-inspection-capacity-refusal-invalid:{role}")
        active = sum(allocation["start"] <= called_at < allocation["end"] for allocation in allocations)
        capacity_refusals.append(
            {
                "called_at": called_at,
                "role": role,
                "attempt": expected_task_names[name][1],
                "active": active,
                "returned_at": returned_at,
                "call_index": parent_rows.index(row),
                "output_index": parent_rows.index(matching[0]),
                "task_name": name,
            }
        )

    used_related_releases: set[tuple[str, str]] = set()
    for refusal in capacity_refusals:
        next_attempts = [
            (later["call_index"], later["called_at"])
            for later in capacity_refusals
            if later["task_name"] == refusal["task_name"] and later["call_index"] > refusal["call_index"]
        ]
        next_attempts.extend(
            (allocation["launch_index"], allocation["start"])
            for allocation in allocations
            if allocation["task_name"] == refusal["task_name"] and allocation["launch_index"] > refusal["call_index"]
        )
        if not next_attempts:
            raise SystemExit(f"review-inspection-capacity-refusal-no-state-change:{refusal['role']}")
        next_call_index, next_call_at = min(next_attempts, key=lambda item: item[0])
        if next_call_index <= refusal["output_index"] or next_call_at <= refusal["returned_at"]:
            raise SystemExit(f"review-inspection-capacity-refusal-no-state-change:{refusal['role']}")
        local_release = any(
            allocation["start"] <= refusal["called_at"] < allocation["end"]
            and refusal["returned_at"] < allocation["end"] <= next_call_at
            for allocation in allocations
        )
        refusal["related_release"] = False
        if not local_release:
            release = _related_capacity_release(codex_home, parent_thread_id, parent_rows, refusal, next_call_at)
            if release is None or release in used_related_releases:
                raise SystemExit(f"review-inspection-capacity-refusal-no-state-change:{refusal['role']}")
            used_related_releases.add(release)
            refusal["related_release"] = True
        # A shared-pool release proves one refill opportunity, never a count of global active children.
        refusal["pool_floor"] = max(1, refusal["active"]) if refusal["related_release"] else refusal["active"]

    capacity_limited = False
    for refusal in sorted(capacity_refusals, key=lambda entry: (entry["called_at"], entry["call_index"])):
        refused_at = refusal["called_at"]
        role = refusal["role"]
        attempt_number = refusal["attempt"]
        active = refusal["active"]
        returned_at = refusal["returned_at"]
        if (active < 1 and not refusal["related_release"]) or active > 4:
            raise SystemExit(f"review-inspection-capacity-refusal-invalid:{role}")
        next_allocation = next(
            item for item in allocations if item["role"] == role and item["attempt"] == attempt_number
        )
        position = ordered_roles.index(role)
        if (
            next_allocation["start"] <= returned_at
            or (
                not refusal["related_release"]
                and not any(returned_at <= allocation["end"] <= next_allocation["start"] for allocation in allocations)
            )
            or (
                attempt_number == 1
                and (
                    first_launches[role][0] <= refused_at
                    or any(first_launches[previous][0] > refused_at for previous in ordered_roles[:position])
                    or any(first_launches[later][0] <= refused_at for later in ordered_roles[position + 1 :])
                )
            )
            or (
                attempt_number > 1
                and not any(
                    allocation["role"] == role
                    and allocation["attempt"] == attempt_number - 1
                    and allocation["end"] <= refused_at
                    for allocation in allocations
                )
            )
        ):
            raise SystemExit(f"review-inspection-capacity-refusal-invalid:{role}")
        capacity_limited = capacity_limited or active < 4

    wait_events: list[tuple[datetime, datetime, int]] = []
    for row in parent_rows:
        payload = row.get("payload")
        if (
            row.get("type") != "response_item"
            or not isinstance(payload, dict)
            or payload.get("type") != "function_call"
            or payload.get("name") != "wait_agent"
        ):
            continue
        matching = [
            candidate
            for candidate in parent_rows
            if candidate.get("type") == "response_item"
            and isinstance(candidate.get("payload"), dict)
            and candidate["payload"].get("type") == "function_call_output"
            and candidate["payload"].get("call_id") == payload.get("call_id")
        ]
        if len(matching) != 1 or "output" not in matching[0]["payload"]:
            raise SystemExit("review-inspection-schedule-wait-invalid")
        try:
            wait_arguments = json.loads(payload.get("arguments", "{}"))
        except (TypeError, json.JSONDecodeError) as error:
            raise SystemExit("review-inspection-schedule-wait-invalid") from error
        timeout = wait_arguments.get("timeout_ms") if isinstance(wait_arguments, dict) else None
        if wait_arguments not in ({}, {"timeout_ms": timeout}) or (
            "timeout_ms" in wait_arguments and (type(timeout) is not int or not 10000 <= timeout <= 3600000)
        ):
            raise SystemExit("review-inspection-schedule-wait-invalid")
        started, returned = _parent_timestamp(row), _parent_timestamp(matching[0])
        if started >= returned:
            raise SystemExit("review-inspection-schedule-wait-invalid")
        call_index = parent_rows.index(row)
        wait_events.append((started, returned, call_index))
        output_index = parent_rows.index(matching[0])
        if any(call_index < allocation["launch_index"] < output_index for allocation in allocations):
            raise SystemExit("review-inspection-schedule-wait-invalid")

    first_roles = set(first_launches)
    repaired_roles = {item["role"] for item in passes if item.get("recovery") is not None}
    first_current_launch = min((allocation["launch_index"] for allocation in allocations), default=len(parent_rows))
    for started, _returned, call_index in wait_events:
        pending = any(
            (allocation["attempt"] == 1 or allocation["role"] not in repaired_roles) and allocation["start"] > started
            for allocation in allocations
        ) or any(role not in first_roles or first_launches[role][0] > started for role in ordered_roles)
        active = sum(allocation["start"] <= started < allocation["end"] for allocation in allocations)
        refusals = [entry for entry in capacity_refusals if entry["called_at"] <= started]
        observed_full_pool = max(refusals, key=lambda entry: entry["called_at"])["pool_floor"] if refusals else 4
        # Earlier dependent-wave waits remain validated but are not this wave's refill opportunities.
        if call_index >= first_current_launch and pending and active < observed_full_pool:
            raise SystemExit("review-inspection-refill-opportunity-missed")
        if any(started <= allocation["start"] < _returned for allocation in allocations):
            raise SystemExit("review-inspection-schedule-wait-invalid")

    # A fast child may finish before the next spawn. Functional parent work, rather than elapsed host time,
    # distinguishes an interrupted free-capacity queue from consecutive generated launches.
    first_index = min(index for _started, index in first_launches.values())
    last_index = max(index for _started, index in first_launches.values())
    launch_indices = {allocation["launch_index"] for allocation in allocations if allocation["attempt"] == 1}
    refusal_indices = {entry["call_index"] for entry in capacity_refusals if entry["attempt"] == 1}
    for index in range(first_index + 1, last_index):
        row = parent_rows[index]
        payload = row.get("payload")
        if (
            row.get("type") != "response_item"
            or not isinstance(payload, dict)
            or payload.get("type") not in {"function_call", "custom_tool_call"}
            or index in launch_indices | refusal_indices
        ):
            continue
        called_at = _parent_timestamp(row)
        active = sum(allocation["start"] <= called_at < allocation["end"] for allocation in allocations)
        refusals = [entry for entry in capacity_refusals if entry["call_index"] < index]
        full_pool = max(refusals, key=lambda entry: entry["call_index"])["pool_floor"] if refusals else 4
        if active < full_pool:
            raise SystemExit("review-inspection-dispatch-interrupted")

    events = sorted(
        (event for item in allocations for event in ((item["start"], 1), (item["end"], -1))),
        key=lambda event: (event[0], event[1]),
    )
    active = peak = 0
    for _timestamp, change in events:
        active += change
        peak = max(peak, active)
    if peak > 4:
        raise SystemExit("review-inspection-active-capacity-exceeded")
    return capacity_limited


def _native_independent_wave(manifest: dict[str, Any], summary: dict[str, Any]) -> bool:
    """Admit a current native launch queue after full runtime validation, independent of core-role policy flags."""
    return (
        manifest.get("schema_version") in {7, 8}
        and manifest.get("manifest_kind") == "native-wave"
        and manifest.get("dispatch_protocol") in {"paged-context-v7", "paged-context-v8"}
        and summary.get("actual_mode") == "independent-spawned"
    )


def _validate_instruction_bounded_review(
    out_dir: Path,
    manifest: dict[str, Any],
    passes: list[dict[str, Any]],
    codex_home: Path,
    parent_thread_id: str,
    roles_dir: Path,
) -> dict[str, object]:
    """Validate supplied-context inspection without claiming host-enforced isolation."""
    if manifest.get("runtime_execution") is not None or manifest.get("app_server_execution") is not None:
        raise SystemExit("review-inspection-runtime-evidence-forbidden")
    _, frozen_contexts, independent_required, _ = _validate_inspection_plan(out_dir, manifest, parent_thread_id)
    inspections = [item for item in passes if item.get("mode") == "inspection"]
    if {item["role"] for item in inspections} != set(frozen_contexts):
        raise SystemExit("review-inspection-context-role-set-mismatch")
    if not inspections:
        return {
            "actual_mode": "serial-fallback",
            "evidence_level": "instruction-bounded-review",
            "write_parallel_eligible": False,
            "independence_satisfied": False,
            "independence_required": independent_required,
            "observed_controls": {},
        }

    parent_rows = _read_jsonl(_find_rollout(codex_home, parent_thread_id))
    capacity_limited = (
        _validate_native_schedule(
            out_dir,
            manifest,
            inspections,
            parent_rows,
            frozen_contexts,
            codex_home,
            parent_thread_id,
            roles_dir,
        )
        if (
            manifest.get("schema_version") in {7, 8}
            and manifest.get("manifest_kind") == "native-wave"
            and manifest.get("dispatch_protocol") in {"paged-context-v7", "paged-context-v8"}
        )
        else False
    )
    controls: dict[str, dict[str, str]] = {}
    intervals: list[tuple[int | float, int | float]] = []
    selected_intervals: list[tuple[int | float, int | float]] = []
    for item in inspections:
        role = item["role"]
        role_card_path = roles_dir / role / "ROLE.md"
        role_card = role_card_path.read_bytes().decode("utf-8")
        selected = item["selected_attempt"]
        for attempt in item["attempts"]:
            context_path = _resolve_path(out_dir, attempt["context_path"])
            if context_path != frozen_contexts[role]:
                raise SystemExit(f"review-inspection-attempt-context-mismatch:{role}")
            context = context_path.read_bytes().decode("utf-8")
            if any(pattern.search(context) for pattern in _SECRET_PATTERNS):
                raise SystemExit(f"review-inspection-context-sensitive-material:{role}")
            if not context.startswith(role_card):
                raise SystemExit(f"review-inspection-role-card-context-missing:{role}")
            child_rows = _read_jsonl(_find_rollout(codex_home, attempt["agent_thread_id"]))
            if manifest.get("schema_version") in {7, 8}:
                turn_id = attempt.get("turn_id")
                terminals = [
                    event for event in _event_payloads(child_rows, "task_complete") if event.get("turn_id") == turn_id
                ]
                if not isinstance(turn_id, str) or not turn_id or len(terminals) != 1:
                    raise SystemExit(f"review-inspection-attempt-timing-missing:{role}:{attempt['attempt']}")
                started_at, completed_at = terminals[0].get("started_at"), terminals[0].get("completed_at")
                if (
                    isinstance(started_at, bool)
                    or isinstance(completed_at, bool)
                    or not isinstance(started_at, int | float)
                    or not isinstance(completed_at, int | float)
                    or not math.isfinite(started_at)
                    or not math.isfinite(completed_at)
                    or started_at >= completed_at
                ):
                    raise SystemExit(f"review-inspection-attempt-timing-invalid:{role}:{attempt['attempt']}")
                intervals.append((started_at, completed_at))
            if _paged_native_manifest(manifest):
                plan_path = _resolve_path(out_dir, manifest["inspection_execution"]["plan_path"])
                recovery = item.get("recovery")
                if recovery is not None and (
                    (recovery["kind"] == "incomplete-dispatch" and attempt["attempt"] == 1)
                    or (
                        recovery["kind"] in {"closure-evidence-shape", "assessment-format", "finding-id-namespace"}
                        and attempt["attempt"] == 2
                    )
                ):
                    if recovery["kind"] in {
                        "closure-evidence-shape",
                        "assessment-format",
                        "finding-id-namespace",
                    } and _inspection_child_called_tool(child_rows):
                        raise SystemExit(f"review-repair-formatting-tool-use:{role}")
                    _recovery_arguments(out_dir, manifest, item, parent_rows, codex_home)
                else:
                    _validate_context_read(child_rows, plan_path, role, attempt, manifest, context)
            elif _inspection_child_called_tool(child_rows):
                raise SystemExit(f"review-inspection-child-tool-use:{role}")
        attempt = item["attempts"][selected - 1]
        context = _resolve_path(out_dir, attempt["context_path"]).read_bytes().decode("utf-8")
        spawn_calls = [
            row["payload"]
            for row in parent_rows
            if row.get("type") == "response_item"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("type") == "function_call"
            and row["payload"].get("name") == "spawn_agent"
        ]
        matching_calls = []
        for call in spawn_calls:
            try:
                arguments = json.loads(call.get("arguments", ""))
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(arguments, dict)
                and call.get("call_id") == attempt.get("spawn_call_id")
                and (_paged_native_manifest(manifest) or arguments.get("message") == context)
                and arguments.get("task_name") == Path(attempt["agent_path"]).name
                and arguments.get("fork_turns") == "none"
                and (
                    not _paged_native_manifest(manifest)
                    or (
                        arguments.get("agent_type", "default") == "default"
                        and arguments.get("model") == attempt["model"]
                        and arguments.get("reasoning_effort") == attempt["effort"]
                    )
                )
            ):
                matching_calls.append(call)
        if len(matching_calls) != 1:
            raise SystemExit(f"review-inspection-context-not-sent:{role}")
        child_rows = _read_jsonl(_find_rollout(codex_home, attempt["agent_thread_id"]))
        controls[role] = _child_controls(child_rows, attempt["turn_id"])
        completions = [
            event
            for event in _event_payloads(child_rows, "task_complete")
            if event.get("turn_id") == attempt["turn_id"]
        ]
        if len(completions) != 1:
            raise SystemExit(f"review-inspection-child-terminal-missing:{role}")
        started_at, completed_at = completions[0].get("started_at"), completions[0].get("completed_at")
        if (
            isinstance(started_at, bool)
            or isinstance(completed_at, bool)
            or not isinstance(started_at, int | float)
            or not isinstance(completed_at, int | float)
            or not math.isfinite(started_at)
            or not math.isfinite(completed_at)
            or started_at >= completed_at
        ):
            raise SystemExit(f"review-inspection-child-timing-invalid:{role}")
        message = _resolve_path(out_dir, attempt["output_path"]).read_text(encoding="utf-8").strip()
        if any(pattern.search(message) for pattern in _SECRET_PATTERNS):
            raise SystemExit(f"review-inspection-output-sensitive-material:{role}")
        if manifest.get("schema_version") in {7, 8}:
            joined = _joined_terminal_timestamp(parent_rows, attempt["agent_path"], message) is not None
        else:
            joined = _joined_terminal_result(parent_rows, attempt["agent_path"], message)
        if not joined:
            raise SystemExit(f"review-inspection-parent-join-missing:{role}")
        if manifest.get("schema_version") not in {7, 8}:
            intervals.append((started_at, completed_at))
        if item.get("recovery") is not None:
            original = item["attempts"][0]
            original_rows = _read_jsonl(_find_rollout(codex_home, original["agent_thread_id"]))
            inspected_source = item["recovery"]["kind"] in {
                "closure-evidence-shape",
                "assessment-format",
                "finding-id-namespace",
            } or any(
                "codex-review-provenance" in json.dumps(row.get("payload", {}).get("output"))
                for row in original_rows
                if row.get("type") == "response_item"
            )
            if inspected_source:
                # Already validated original reads prove actual source concurrency; only a2 supplies accepted coverage.
                original_terminal = next(
                    event
                    for event in _event_payloads(original_rows, "task_complete")
                    if event.get("turn_id") == original["turn_id"]
                )
                selected_intervals.append((original_terminal["started_at"], original_terminal["completed_at"]))
            else:
                selected_intervals.append((started_at, completed_at))
        else:
            selected_intervals.append((started_at, completed_at))

    overlaps = any(
        max(first[0], second[0]) < min(first[1], second[1])
        for index, first in enumerate(selected_intervals)
        for second in selected_intervals[index + 1 :]
    )
    if manifest.get("schema_version") in {7, 8}:
        active = peak_active = 0
        events = sorted(
            (event for start, end in intervals for event in ((start, 1), (end, -1))),
            key=lambda event: (event[0], event[1]),
        )
        for _timestamp, change in events:
            active += change
            peak_active = max(peak_active, active)
        if peak_active > 4:
            raise SystemExit("review-inspection-active-capacity-exceeded")
    actual_mode = "parallel" if overlaps else "independent-spawned" if len(inspections) > 1 else "serial"
    required_roles = REQUIRED_ROLES & {item["role"] for item in passes}
    independence_satisfied = bool(required_roles) and required_roles <= {item["role"] for item in inspections}
    return {
        "actual_mode": actual_mode,
        "evidence_level": "instruction-bounded-review",
        "write_parallel_eligible": False,
        "capacity_limited": capacity_limited,
        "independence_satisfied": independence_satisfied,
        "independence_required": independent_required,
        "observed_controls": controls,
    }


def _text_reviewer_assessment(body: str, *, batch_response: bool) -> tuple[int, str] | None:
    """Parse a standalone ordinary pair or the strict batch body, retaining opaque one-line rationale."""
    if not batch_response:
        lines = body.splitlines()
        declarations = []
        fence = None
        for index, line in enumerate(lines):
            marker = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
            if marker:
                token = marker[1]
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence):
                    fence = None
            if re.match(r"^[ \t]*(?:Rating|Rationale):", line):
                # Reserved declarations count even when malformed or quoted as a fenced example.
                if fence is not None:
                    return None
                declarations.append(index)
        if len(declarations) != 2:
            return None
        first, last = declarations
        # Blank paragraph boundaries distinguish surrounding content from a continued rationale.
        if (
            (first and lines[first - 1].strip())
            or (last + 1 < len(lines) and lines[last + 1].strip())
            or any(line.strip() for line in lines[first + 1 : last])
        ):
            return None
        body = "\n".join(lines[first : last + 1])
    separator = r"(?:[ \t]*\r?\n\s*|\.?[ \t]+)" if batch_response else r"[ \t]*\r?\n\s*"
    assessment = re.fullmatch(r"\s*Rating: ([1-5])" + separator + r"Rationale: (\S[^\r\n]*)[ \t]*(?:\r?\n)*", body)
    return (int(assessment[1]), assessment[2]) if assessment is not None else None


def _retained_reviewer_rating(
    path: Path,
    *,
    local_reviewer_wave: bool,
    main: bool,
    role: str,
    structured_native: bool = False,
    preassessment_blocker: bool = False,
    incomplete_dispatch: bool = False,
    batch_response: bool = False,
) -> int:
    """Read a scoped rating and rationale from the retained reviewer response.

    Proven preassessment recovery may retain paired unavailable digests only for an empty blocking response. Completed
    inspection retains the strict digest contract. A proved partial-dispatch original may use an equivalent bold
    assessment heading; completed reviewers still require the canonical heading.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SystemExit(f"review-assessment-content-invalid:{role}") from error
    if local_reviewer_wave or structured_native:
        if structured_native:
            block = re.fullmatch(r"```adversarial-loop\n(.*?)\n```", content.strip(), re.DOTALL)
            content = block.group(1) if block else content
        try:
            payload = json.loads(content)
        except (ValueError, RecursionError) as error:
            raise SystemExit(f"review-assessment-content-invalid:{role}") from error
        assessment = payload.get("assessment") if isinstance(payload, dict) else None
        if structured_native:
            if (
                not isinstance(payload, dict)
                or set(payload) != {"source_sha256", "diff_sha256", "findings", "assessment"}
                or not isinstance(payload["findings"], list)
                or not isinstance(assessment, dict)
                or set(assessment) != {"rating", "rationale"}
            ):
                raise SystemExit(f"review-assessment-content-invalid:{role}")
            # Failed first reads cannot supply source digests; this receipt never certifies inspection.
            unavailable = (
                preassessment_blocker
                and payload["source_sha256"] is None
                and payload["diff_sha256"] is None
                and payload["findings"] == []
                and assessment["rating"] == 5
            )
            if not unavailable and any(
                not isinstance(payload[key], str) or re.fullmatch(r"[0-9a-f]{64}", payload[key]) is None
                for key in ("source_sha256", "diff_sha256")
            ):
                raise SystemExit(f"review-assessment-content-invalid:{role}")
        rating = assessment.get("rating") if isinstance(assessment, dict) else None
        rationale = assessment.get("rationale") if isinstance(assessment, dict) else None
    else:
        heading = "Main Reviewer Assessment" if main else "Reviewer Assessment"
        assessment_heading = rf"## {re.escape(heading)}"
        if incomplete_dispatch and not main:
            assessment_heading = rf"(?:{assessment_heading}|\*\*{re.escape(heading)}\*\*)"
        if len(re.findall(rf"(?m)^{assessment_heading}[ \t]*$", content)) != 1:
            raise SystemExit(f"review-assessment-content-invalid:{role}")
        boundary = r"^## |\Z"
        if incomplete_dispatch and not main:
            boundary += r"|^\*\*[^*\n]+\*\*[ \t]*$"
        section = re.search(rf"(?ms)^{assessment_heading}\s*\n(?P<body>.*?)(?={boundary})", content)
        body = section.group("body") if section else ""
        assessment = _text_reviewer_assessment(body, batch_response=batch_response and not main)
        rating, rationale = assessment if assessment is not None else (None, None)
    if type(rating) is not int or rating not in range(1, 6) or not isinstance(rationale, str) or not rationale.strip():
        raise SystemExit(f"review-assessment-content-invalid:{role}")
    return rating


def _validate_local_reviewer_wave(
    out_dir: Path,
    manifest: dict[str, Any],
    passes: list[dict[str, Any]],
    *,
    require_assessment: bool = True,
    roles_dir: Path = PLUGIN_ROOT / "roles",
) -> dict[str, object]:
    """Bind an isolated review wave without manufacturing native child lineage."""
    execution = manifest.get("app_server_execution")
    if not isinstance(execution, dict) or set(execution) != {"plan_path", "evidence_path", "evidence_sha256"}:
        raise SystemExit("review-app-server-execution-missing")
    if manifest.get("runtime_execution") is not None or any(item.get("mode") == "spawned" for item in passes):
        raise SystemExit("review-app-server-native-evidence-forbidden")
    plan_path = _resolve_path(out_dir, execution["plan_path"])
    evidence_path = _resolve_path(out_dir, execution["evidence_path"])
    if not evidence_path.is_file() or _sha256(evidence_path) != execution["evidence_sha256"]:
        raise SystemExit("review-app-server-evidence-hash-mismatch")
    try:
        summary = validate_local_reviewer_evidence(
            plan_path,
            evidence_path,
            roles_dir,
            require_dispatch=True,
            require_assessment=require_assessment,
        )
    except (ReviewRouteError, ValueError, OSError) as error:
        raise SystemExit(f"review-app-server-evidence-invalid:{error}") from error
    evidence = _load_json(evidence_path)
    for key in ("review_run_id", "parent_thread_id", "review_input_sha256"):
        if evidence.get(key) != manifest.get(key):
            raise SystemExit(f"review-app-server-identity-mismatch:{key}")
    isolated = [item for item in passes if item.get("mode") == "app-server"]
    nodes = {node["role_id"]: node for node in evidence["nodes"]}
    if not isolated or len(isolated) != len(nodes) or {item.get("role") for item in isolated} != set(nodes):
        raise SystemExit("review-app-server-role-set-mismatch")
    for item in isolated:
        node = nodes[item["role"]]
        if (
            item.get("role_card_sha256") != node["role_card_sha256"]
            or _resolve_path(out_dir, item.get("output_path")) != (evidence_path.parent / node["output_path"]).resolve()
            or item.get("attempts") not in (None, [])
            or item.get("selected_attempt") is not None
        ):
            raise SystemExit(f"review-app-server-pass-mismatch:{item['role']}")
    if (
        summary.get("evidence_level") != "app-server-parent-observed"
        or summary.get("actual_mode") not in {"parallel", "independent-spawned"}
        or summary.get("approval_policy") != "never"
        or summary.get("filesystem_credential_isolation") != "unverified"
        or summary.get("write_parallel_eligible") is not False
        or summary.get("consumer_id") != "code-review"
    ):
        raise SystemExit("review-app-server-summary-invalid")
    return summary


def _receipt_binds_child(
    parent_rows: list[dict[str, Any]],
    codex_home: Path,
    parent_thread_id: str,
    attempt: dict[str, Any],
    context: str,
    *,
    schema_version: int = 5,
    model: str | None = None,
    effort: str | None = None,
) -> bool:
    """Bind a path-only native spawn receipt to one newly created runtime child session.

    This inspection-only route requires call, receipt, and child creation timestamps to prevent a retained older same-
    path child from substituting for a missing new log. Unknown receipt formats, missing timestamps, and ambiguous
    sessions fail closed.
    """
    call_id = attempt.get("spawn_call_id")
    if not isinstance(call_id, str) or not call_id:
        return False
    records = [
        row
        for row in parent_rows
        if row.get("type") == "response_item"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("call_id") == call_id
    ]
    calls = [row for row in records if row["payload"].get("type") == "function_call"]
    receipts = [row for row in records if row["payload"].get("type") == "function_call_output"]
    if len(calls) != 1 or len(receipts) != 1 or calls[0]["payload"].get("name") != "spawn_agent":
        return False
    try:
        arguments = json.loads(calls[0]["payload"].get("arguments", ""))
        receipt = json.loads(receipts[0]["payload"].get("output", ""))
        called_at = datetime.fromisoformat(calls[0]["timestamp"].replace("Z", "+00:00"))
        received_at = datetime.fromisoformat(receipts[0]["timestamp"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    agent_path = attempt["agent_path"]
    if (
        receipt != {"task_name": agent_path}
        or not isinstance(arguments, dict)
        or (schema_version != 6 and arguments.get("message") != context)
        or arguments.get("task_name") != agent_path.rsplit("/", 1)[-1]
        or arguments.get("fork_turns") != "none"
        or (
            schema_version == 6
            and (
                arguments.get("agent_type", "default") != "default"
                or arguments.get("model") != model
                or arguments.get("reasoning_effort") != effort
            )
        )
        or called_at.tzinfo is None
        or received_at.tzinfo is None
        or called_at >= received_at
    ):
        return False

    # The host receipt contains no UUID. Require unique parent/path metadata as well
    # as creation inside this call's interval, never a guessed or synthesized event.
    matches: list[str] = []
    for path in (codex_home / "sessions").rglob("*.jsonl"):
        try:
            with path.open(encoding="utf-8") as stream:
                row = json.loads(stream.readline())
            if not isinstance(row, dict) or row.get("type") != "session_meta":
                return False
            session = row["payload"]
            source = session.get("source")
            if not isinstance(source, dict):
                continue
            subagent = source.get("subagent")
            if not isinstance(subagent, dict):
                continue
            spawn = subagent.get("thread_spawn", {})
            if spawn.get("parent_thread_id") != parent_thread_id or spawn.get("agent_path") != agent_path:
                continue
            if session.get("parent_thread_id") != parent_thread_id or session.get("agent_path") != agent_path:
                return False
            created_at = datetime.fromisoformat(session["timestamp"].replace("Z", "+00:00"))
            if created_at.tzinfo is None or not called_at <= created_at <= received_at:
                return False
            matches.append(session["id"])
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            return False
    if matches != [attempt["agent_thread_id"]]:
        return False
    if schema_version != 6:
        return True
    child_rows = _read_jsonl(_find_rollout(codex_home, attempt["agent_thread_id"]))
    delivered = [
        item["encrypted_content"]
        for row in child_rows
        if row.get("type") == "response_item"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type") == "agent_message"
        for item in row["payload"].get("content", [])
        if isinstance(item, dict) and isinstance(item.get("encrypted_content"), str)
    ]
    return len(delivered) == 1 and delivered[0] == arguments.get("message")


def _closure_shape_repair(content: str, snapshot: dict[str, Any], role: str) -> str:
    """Repair only list-valued closure evidence while retaining every other response byte and finding value."""
    match = re.search(r"\A\s*## Reviewer Findings\s*\n```json\n(.*?)\n```", content, re.DOTALL)
    if match is None:
        raise SystemExit(f"review-repair-ineligible-format:{role}")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Reject duplicate keys rather than changing which claim survives."""
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        records = json.loads(match[1], object_pairs_hook=unique)
    except (ValueError, RecursionError) as error:
        raise SystemExit(f"review-repair-ineligible-json:{role}") from error
    changed = False
    if not isinstance(records, list):
        raise SystemExit(f"review-repair-ineligible-records:{role}")
    for record in records:
        if not isinstance(record, dict):
            raise SystemExit(f"review-repair-ineligible-records:{role}")
        closure = record.get("closure_evidence")
        if isinstance(closure, list):
            if not closure or any(not isinstance(value, str) or not value.strip() for value in closure):
                raise SystemExit(f"review-repair-ineligible-closure:{role}")
            record["closure_evidence"] = "\n".join(closure)
            changed = True
    if not changed:
        raise SystemExit(f"review-repair-no-shape-error:{role}")
    expected = content[: match.start(1)] + json.dumps(records, ensure_ascii=False) + content[match.end(1) :]
    _batch_reviewer_findings(Path("unused"), snapshot, role, content_override=expected)
    return expected


def _assessment_format_repair(content: str, snapshot: dict[str, Any], role: str) -> str:
    """Insert a missing rationale label after one explicit rating without changing any response value."""
    if len(re.findall(r"(?m)^## Reviewer Assessment[ \t]*$", content)) != 1:
        raise SystemExit(f"review-repair-ineligible-assessment:{role}")
    assessment = re.search(
        r"(?m)^## Reviewer Assessment[ \t]*\n\s*Rating: [1-5](?P<separator> — )\S[^\r\n]*(?:\r?\n)*\Z",
        content,
    )
    if assessment is None:
        raise SystemExit(f"review-repair-ineligible-assessment:{role}")
    expected = content[: assessment.start("separator")] + "\nRationale: " + content[assessment.end("separator") :]
    _batch_reviewer_findings(Path("unused"), snapshot, role, content_override=expected)
    return expected


def _finding_id_namespace_repair(
    content: str, snapshot: dict[str, Any], role: str, context: str, origins: list[dict[str, Any]]
) -> str:
    """Restore one source-proven local ID without changing another response byte or obligation."""
    match = re.search(r"\A\s*## Reviewer Findings\s*\n```json\n(.*?)\n```", content, re.DOTALL)
    if match is None:
        raise SystemExit(f"review-repair-ineligible-finding-id:{role}")
    candidates = []
    for origin in origins:
        identity = origin["finding_id"]
        local_id = origin["original"]["id"]
        witness = f"Source finding ID: {identity}\n{origin['original_text']}\n"
        token = re.compile(r'("id"\s*:\s*)' + re.escape(json.dumps(identity)))
        occurrences = list(token.finditer(match[1]))
        if occurrences:
            if (
                len(occurrences) != 1
                or context.count(witness) != 1
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", local_id) is None
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", identity) is not None
            ):
                raise SystemExit(f"review-repair-ineligible-finding-id:{role}")
            candidates.append((origin, occurrences[0]))
    if len(candidates) != 1:
        raise SystemExit(f"review-repair-ineligible-finding-id:{role}")
    origin, token = candidates[0]
    start = match.start(1) + token.end(1)
    end = match.start(1) + token.end()
    expected = content[:start] + json.dumps(origin["original"]["id"]) + content[end:]
    records = _batch_reviewer_findings(Path("unused"), snapshot, role, content_override=expected)
    corrected = next(record for record in records if record["id"] == origin["original"]["id"])
    if corrected != origin["original"]:
        raise SystemExit(f"review-repair-finding-id-claims-changed:{role}")
    return expected


def _original_dispatch_message(
    manifest: dict[str, Any],
    plan_path: Path,
    role: str,
    attempt: dict[str, Any],
    parent_rows: list[dict[str, Any]],
) -> str:
    """Retain dispatch bytes for delivery binding and separately audit malformed page blocks."""
    canonical = _native_dispatch_message(manifest, plan_path, role, 1)
    calls = [
        row["payload"]
        for row in parent_rows
        if row.get("type") == "response_item"
        and row.get("payload", {}).get("type") == "function_call"
        and row["payload"].get("call_id") == attempt.get("spawn_call_id")
    ]
    if len(calls) != 1:
        raise SystemExit(f"review-repair-original-dispatch-missing:{role}")
    try:
        actual = json.loads(calls[0]["arguments"])["message"]
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"review-repair-original-dispatch-invalid:{role}") from error
    if actual == canonical:
        return canonical
    expected_blocks = list(re.finditer(r"```javascript\n(.*?)\n```", canonical, re.DOTALL))
    actual_blocks = (
        list(re.finditer(r"```javascript\n(.*?)\n```", actual, re.DOTALL)) if isinstance(actual, str) else []
    )
    # Opaque hosts expose no recipe blocks; receipt validation still requires exact child delivery, and
    # partial-dispatch eligibility separately proves every executed page and the bounded reader failure.
    if _paged_native_manifest(manifest) and isinstance(actual, str) and actual and not actual_blocks:
        return actual
    normalized = actual
    for expected, observed in reversed(list(zip(expected_blocks, actual_blocks))):
        normalized = normalized[: observed.start(1)] + expected[1] + normalized[observed.end(1) :]
    if not expected_blocks or len(actual_blocks) != len(expected_blocks) or normalized != canonical:
        raise SystemExit(f"review-repair-original-dispatch-invalid:{role}")
    return actual


def _recovery_arguments(
    out_dir: Path,
    manifest: dict[str, Any],
    item: dict[str, Any],
    parent_rows: list[dict[str, Any]],
    codex_home: Path,
) -> dict[str, str]:
    """Derive one correction from observed original provenance, never a parent-authored replacement finding."""
    # Resolve the existing producer/validator circular boundary only after both modules initialize.
    import review_prepare

    role = item["role"]
    kind = item["recovery"]["kind"]
    if kind not in {"closure-evidence-shape", "assessment-format", "finding-id-namespace", "incomplete-dispatch"}:
        raise SystemExit(f"review-repair-kind-invalid:{role}")
    if kind in {"assessment-format", "finding-id-namespace"} and (
        manifest.get("schema_version") != 8 or out_dir.parent.name != "batches"
    ):
        raise SystemExit(f"review-repair-ineligible-assessment:{role}")
    original = item["attempts"][0]
    card = _load_role_card(PLUGIN_ROOT / "roles", role)
    # The original response must independently bind to its real child, parent and exact output bytes.
    initial = {**item, "attempts": [original], "selected_attempt": 1, "output_path": original["output_path"]}
    initial.pop("recovery", None)
    _validate_spawn_attempts(
        out_dir,
        initial,
        manifest,
        codex_home,
        parent_rows,
        set(),
        card,
        set(),
        set(),
        original_dispatch_failure=kind == "incomplete-dispatch",
    )
    rows = _read_jsonl(_find_rollout(codex_home, original["agent_thread_id"]))
    raw = _resolve_path(out_dir, original["raw_output_path"]).read_bytes().decode("utf-8")
    if _joined_terminal_timestamp(parent_rows, original["agent_path"], raw.strip()) is None:
        raise SystemExit(f"review-repair-original-not-joined:{role}")
    context = _resolve_path(out_dir, original["context_path"]).read_text(encoding="utf-8")
    plan_path = _resolve_path(out_dir, manifest["inspection_execution"]["plan_path"])
    if kind in {"closure-evidence-shape", "assessment-format", "finding-id-namespace"}:
        _validate_context_read(rows, plan_path, role, original, manifest, context)
        snapshot = _load_json(out_dir.parent.parent / "batch-inventory.json")["source_snapshot"]
        if kind == "finding-id-namespace":
            import review_batches  # Reuse the verified circular boundary to admit immutable source-origin witnesses.

            root = out_dir.parent.parent
            inventory = review_batches.validate_inventory(root)
            review_batches._admit_waves(root, inventory, review_batches._source_wave_paths(root, inventory), codex_home)
            expected = _finding_id_namespace_repair(
                raw, snapshot, role, context, review_batches._source_ledger(root, inventory)
            )
        elif kind == "closure-evidence-shape":
            expected = _closure_shape_repair(raw, snapshot, role)
        else:
            expected = _assessment_format_repair(raw, snapshot, role)
        message = (
            (
                "Correct only closure_evidence arrays in the following retained response. Join their strings with a newline "
                "in original order. Preserve every other finding field, claim, severity, evidence, confidence and assessment "
                if kind == "closure-evidence-shape"
                else "Restore only the supplied source origin's proven reviewer-local finding ID. Preserve every "
                "other finding field, claim, severity, evidence, confidence, assessment and other bytes "
                if kind == "finding-id-namespace"
                else "Insert only the missing Rationale label and line separation after the explicit rating. Preserve "
                "the exact rating, explanation, every finding field, claim, severity, evidence, confidence and other bytes "
            )
            + "exactly. Use no tools and perform no source reassessment. Return exactly this validated correction:\n"
            + expected
        )
    elif any(
        "codex-review-provenance" in json.dumps(row.get("payload", {}).get("output"))
        for row in rows
        if row.get("type") == "response_item"
    ):
        _validate_context_read(rows, plan_path, role, original, manifest, context, incomplete_dispatch=True)
        calls = [
            row["payload"]
            for row in rows
            if row.get("type") == "response_item"
            and row.get("payload", {}).get("type") in {"custom_tool_call", "function_call"}
        ]
        canonical = _native_dispatch_message(manifest, plan_path, role, 1)
        actual = _original_dispatch_message(manifest, plan_path, role, original, parent_rows)
        expected_blocks = re.findall(r"```javascript\n(.*?)\n```", canonical, re.DOTALL)
        actual_blocks = re.findall(r"```javascript\n(.*?)\n```", actual, re.DOTALL)
        if any(
            observed != expected
            and (
                page >= len(calls)
                or observed not in {calls[page].get("input"), calls[page].get("input", "").rstrip("\n")}
            )
            for page, (expected, observed) in enumerate(zip(expected_blocks, actual_blocks))
        ):
            raise SystemExit(f"review-repair-original-dispatch-invalid:{role}")
        if _retained_reviewer_rating(
            _resolve_path(out_dir, original["output_path"]),
            local_reviewer_wave=False,
            main=False,
            role=role,
            structured_native=_load_json(plan_path)["consumer_policy"]["consumer_id"] == "challenge-resolve",
            incomplete_dispatch=True,
        ) not in {4, 5}:
            raise SystemExit(f"review-repair-incomplete-assessment-missing:{role}")
        message = _native_dispatch_message(manifest, plan_path, role, 2)
    else:
        calls = [
            row["payload"]
            for row in rows
            if row.get("type") == "response_item"
            and row.get("payload", {}).get("type") in {"custom_tool_call", "function_call"}
        ]
        canonical = _native_dispatch_message(manifest, plan_path, role, 1)
        actual = _original_dispatch_message(manifest, plan_path, role, original, parent_rows)
        expected_blocks = re.findall(r"```javascript\n(.*?)\n```", canonical, re.DOTALL)
        actual_blocks = re.findall(r"```javascript\n(.*?)\n```", actual, re.DOTALL)
        if (
            not actual_blocks
            or actual_blocks[1:] != expected_blocks[1:]
            or (
                actual_blocks[0] != expected_blocks[0]
                and not any(actual_blocks[0] == call.get("input", "").rstrip("\n") for call in calls)
            )
        ):
            raise SystemExit(f"review-repair-original-dispatch-invalid:{role}")
        expected_call = render_read_call(
            plan_path,
            role,
            1,
            manifest["context_reader_python"],
            reader_path=Path(manifest["context_reader_path"]),
            _include_workdir=manifest.get("schema_version") == 6
            or manifest.get("context_reader_sha256") in LEGACY_WORKDIR_READER_SHA256S,
        )
        expected_call = _native_read_frame(manifest, plan_path, role, original, 1, expected_call)
        if not calls or any(
            call.get("name") != "exec" or call.get("input") in {expected_call, expected_call + "\n"} for call in calls
        ):
            raise SystemExit(f"review-repair-dispatch-cause-unproven:{role}")
        if (
            _retained_reviewer_rating(
                _resolve_path(out_dir, original["output_path"]),
                local_reviewer_wave=False,
                main=False,
                role=role,
                structured_native=_load_json(plan_path)["consumer_policy"]["consumer_id"] == "challenge-resolve",
                preassessment_blocker=True,
            )
            != 5
        ):
            raise SystemExit(f"review-repair-preassessment-blocker-missing:{role}")
        tool_rows = [
            row["payload"]
            for row in rows
            if row.get("type") == "response_item"
            and row.get("payload", {}).get("type")
            in {"custom_tool_call", "function_call", "custom_tool_call_output", "function_call_output"}
        ]
        if len(tool_rows) != 2 * len(calls):
            raise SystemExit(f"review-repair-dispatch-results-missing:{role}")
        for index, call in enumerate(calls):
            output = tool_rows[2 * index + 1]
            diagnostic = json.dumps(output.get("output"))
            if (
                tool_rows[2 * index] != call
                or output.get("call_id") != call.get("call_id")
                or ("usage:" not in diagnostic or "error:" not in diagnostic)
            ):
                raise SystemExit(f"review-repair-dispatch-failure-unproven:{role}")
            try:
                input_args = review_prepare._read_arguments(call["input"])
                expected_args = review_prepare._read_arguments(expected_call)
            except (ValueError, IndexError, KeyError, TypeError) as error:
                raise SystemExit(f"review-repair-dispatch-cause-unproven:{role}") from error
            if (
                set(input_args) != set(expected_args)
                or any(input_args[key] != expected_args[key] for key in expected_args if key != "cmd")
                or not isinstance(input_args["cmd"], str)
            ):
                raise SystemExit(f"review-repair-dispatch-cause-unproven:{role}")
            rendered = expected_call.replace(
                json.dumps(expected_args, ensure_ascii=False), json.dumps(input_args, ensure_ascii=False)
            )
            reader_prefix = expected_args["cmd"].split(" --plan", 1)[0]
            producer_prefix = reader_prefix.replace("review_context.py", "review_prepare.py")
            if call["input"] not in {rendered, rendered + "\n"} or (
                not any(input_args["cmd"].startswith(prefix + " ") for prefix in (reader_prefix, producer_prefix))
                or any(character in input_args["cmd"] for character in (";", "|", "&", "`", "$", "<", ">", "\n", "\r"))
            ):
                raise SystemExit(f"review-repair-dispatch-cause-unproven:{role}")
        commands = [
            row["payload"]["item"]
            for row in rows
            if row.get("type") == "event_msg"
            and row.get("payload", {}).get("type") == "item_completed"
            and row.get("payload", {}).get("item", {}).get("type") == "CommandExecution"
        ]
        if (commands or manifest.get("dispatch_protocol") == "paged-context-v8") and (
            len(commands) != len(calls)
            or any(
                command.get("exit_code") != 2
                or not _reader_command_matches(
                    command.get("command"), review_prepare._read_arguments(call["input"])["cmd"]
                )
                for command, call in zip(commands, calls)
            )
        ):
            raise SystemExit(f"review-repair-dispatch-failure-unproven:{role}")
        message = _native_dispatch_message(manifest, plan_path, role, 2)
    return {
        "task_name": f"review_{role.replace('-', '_')}_{original['context_sha256'][:12]}_a2",
        "fork_turns": "none",
        "model": card["model"],
        "reasoning_effort": card["model_reasoning_effort"],
        "message": message,
    }


def _validate_spawn_attempts(
    out_dir: Path,
    item: dict[str, Any],
    manifest: dict[str, Any],
    codex_home: Path,
    parent_rows: list[dict[str, Any]],
    used_threads: set[str],
    role_card: dict[str, str],
    used_context_paths: set[Path],
    used_output_paths: set[Path],
    *,
    original_dispatch_failure: bool = False,
) -> None:
    """Bind a spawned specialist output to parent and child rollout evidence."""
    role = str(item["role"])
    attempts = item.get("attempts")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= 2:
        raise SystemExit(f"manifest-invalid-attempt-count:{role}")
    if not all(isinstance(attempt, dict) for attempt in attempts):
        raise SystemExit(f"manifest-attempt-not-object:{role}")
    if [attempt.get("attempt") for attempt in attempts] != list(range(1, len(attempts) + 1)):
        raise SystemExit(f"manifest-attempt-sequence:{role}")
    recovery = item.get("recovery")
    if recovery is not None and (
        manifest.get("schema_version") != 8
        or len(attempts) != 2
        or recovery
        not in (
            {"kind": "closure-evidence-shape"},
            {"kind": "assessment-format"},
            {"kind": "finding-id-namespace"},
            {"kind": "incomplete-dispatch"},
        )
        or item.get("selected_attempt") != 2
        or any(attempt.get("status") != "completed" for attempt in attempts)
    ):
        raise SystemExit(f"manifest-invalid-internal-recovery:{role}")
    if (
        recovery is None
        and len(attempts) == 2
        and (attempts[0].get("status") == "completed" or attempts[0].get("error_type") not in TRANSIENT_RETRY_ERRORS)
    ):
        raise SystemExit(f"manifest-invalid-retry:{role}")
    selected = item.get("selected_attempt")
    if not isinstance(selected, int) or selected < 1 or selected > len(attempts):
        raise SystemExit(f"manifest-invalid-selected-attempt:{role}")

    parent_events = _event_payloads(parent_rows, "sub_agent_activity")
    role_context_paths: set[Path] = set()
    for attempt in attempts:
        thread_id = attempt.get("agent_thread_id")
        event_id = attempt.get("event_id")
        agent_path = attempt.get("agent_path")
        receipt_route = (
            manifest.get("schema_version") in {5, 6, 7, 8} or _paged_native_manifest(manifest)
        ) and "event_id" not in attempt
        identities = (thread_id, agent_path) if receipt_route else (thread_id, event_id, agent_path)
        if not all(isinstance(value, str) and value for value in identities):
            raise SystemExit(f"manifest-attempt-identity-missing:{role}")
        context_path = _resolve_path(out_dir, attempt.get("context_path"))
        if context_path in used_context_paths and not (
            (manifest.get("schema_version") in {5, 6, 7, 8} or _paged_native_manifest(manifest))
            and context_path in role_context_paths
        ):
            raise SystemExit("manifest-reused-context-path")
        used_context_paths.add(context_path)
        role_context_paths.add(context_path)
        context_sha256 = attempt.get("context_sha256")
        if not context_path.exists() or _sha256(context_path) != context_sha256:
            raise SystemExit(f"provenance-context-hash-mismatch:{role}")
        expected_agent_name = f"review_{role.replace('-', '_')}_{context_sha256[:12]}_a{attempt['attempt']}"
        if Path(agent_path).name != expected_agent_name:
            raise SystemExit(f"provenance-agent-path-context-mismatch:{role}:{agent_path}")
        if thread_id in used_threads:
            raise SystemExit(f"manifest-reused-agent-thread:{thread_id}")
        used_threads.add(thread_id)
        matches = [
            event
            for event in parent_events
            if event.get("event_id") == event_id
            and event.get("agent_thread_id") == thread_id
            and event.get("agent_path") == agent_path
            and event.get("kind") == "started"
        ]
        if receipt_route:
            sent_context = (
                _native_dispatch_message(
                    manifest,
                    _resolve_path(out_dir, manifest["inspection_execution"]["plan_path"]),
                    role,
                    attempt["attempt"],
                )
                if _paged_native_manifest(manifest)
                else context_path.read_bytes().decode("utf-8")
            )
            if recovery is not None and attempt["attempt"] == 2:
                sent_context = _recovery_arguments(out_dir, manifest, item, parent_rows, codex_home)["message"]
            elif original_dispatch_failure or (recovery == {"kind": "incomplete-dispatch"} and attempt["attempt"] == 1):
                sent_context = _original_dispatch_message(
                    manifest,
                    _resolve_path(out_dir, manifest["inspection_execution"]["plan_path"]),
                    role,
                    attempt,
                    parent_rows,
                )
            bound = _receipt_binds_child(
                parent_rows,
                codex_home,
                manifest["parent_thread_id"],
                attempt,
                sent_context,
                schema_version=6 if _paged_native_manifest(manifest) else manifest["schema_version"],
                model=role_card["model"],
                effort=role_card["model_reasoning_effort"],
            )
            if manifest.get("schema_version") in {7, 8} and not _paged_native_manifest(manifest):
                sent_calls = [
                    row["payload"]
                    for row in parent_rows
                    if row.get("type") == "response_item"
                    and isinstance(row.get("payload"), dict)
                    and row["payload"].get("type") == "function_call"
                    and row["payload"].get("call_id") == attempt.get("spawn_call_id")
                ]
                bound = (
                    bound
                    and len(sent_calls) == 1
                    and json.loads(sent_calls[0]["arguments"]).get("message") == sent_context
                )
        else:
            bound = len(matches) == 1
        if not bound:
            raise SystemExit(f"provenance-parent-spawn-mismatch:{role}:{attempt['attempt']}")

        child_rows = _read_jsonl(_find_rollout(codex_home, thread_id))
        session_rows = [
            row["payload"]
            for row in child_rows
            if row.get("type") == "session_meta"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("id") == thread_id
        ]
        if len(session_rows) != 1:
            raise SystemExit(f"provenance-child-session-count:{thread_id}")
        session = session_rows[0]
        spawn = session.get("source", {}).get("subagent", {}).get("thread_spawn", {})
        if session.get("id") != thread_id or spawn.get("parent_thread_id") != manifest["parent_thread_id"]:
            raise SystemExit(f"provenance-child-parent-mismatch:{thread_id}")
        session_path = session.get("agent_path") or spawn.get("agent_path")
        if session_path != agent_path:
            raise SystemExit(f"provenance-child-path-mismatch:{role}:{session_path}")
        session_role = session.get("agent_role") or spawn.get("agent_role")
        if session_role is not None and session_role != ("default" if _paged_native_manifest(manifest) else role):
            raise SystemExit(f"provenance-child-role-mismatch:{role}:{session_role}")

        if attempt.get("status") != "completed":
            if attempt.get("error_type") not in TRANSIENT_RETRY_ERRORS or attempt["attempt"] == selected:
                raise SystemExit(f"manifest-invalid-failed-attempt:{role}:{attempt['attempt']}")
            continue

        turn_id = attempt.get("turn_id")
        contexts = [
            row["payload"]
            for row in child_rows
            if row.get("type") == "turn_context"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("turn_id") == turn_id
        ]
        if len(contexts) != 1:
            raise SystemExit(f"provenance-turn-context-mismatch:{thread_id}")
        context = contexts[0]
        if context.get("model") != attempt.get("model") or context.get("effort") != attempt.get("effort"):
            raise SystemExit(f"provenance-model-effort-mismatch:{thread_id}")
        if context.get("model") != role_card["model"]:
            raise SystemExit(f"provenance-role-model-policy-mismatch:{role}:{thread_id}")
        if context.get("effort") != role_card["model_reasoning_effort"]:
            raise SystemExit(f"provenance-role-effort-policy-mismatch:{role}:{thread_id}")
        completions = [
            event for event in _event_payloads(child_rows, "task_complete") if event.get("turn_id") == turn_id
        ]
        if len(completions) != 1 or not isinstance(completions[0].get("last_agent_message"), str):
            raise SystemExit(f"provenance-task-complete-mismatch:{thread_id}")

        output_path = _resolve_path(out_dir, attempt.get("output_path"))
        if output_path in used_output_paths:
            raise SystemExit("manifest-reused-output-path")
        used_output_paths.add(output_path)
        if not output_path.exists() or _sha256(output_path) != attempt.get("output_sha256"):
            raise SystemExit(f"provenance-output-hash-mismatch:{role}")
        if manifest.get("schema_version") in {7, 8}:
            raw = completions[0]["last_agent_message"].encode("utf-8")
            raw_path = _resolve_path(out_dir, attempt.get("raw_output_path"))
            if raw_path.read_bytes() != raw or _sha256(raw_path) != attempt.get("raw_output_sha256"):
                raise SystemExit(f"provenance-raw-output-mismatch:{role}")
            if recovery is not None and attempt["attempt"] == 2:
                expected_arguments = _recovery_arguments(out_dir, manifest, item, parent_rows, codex_home)
                if recovery["kind"] in {"closure-evidence-shape", "assessment-format", "finding-id-namespace"}:
                    expected_output = expected_arguments["message"].split(
                        "Return exactly this validated correction:\n", 1
                    )[1]
                    if raw.decode("utf-8") != expected_output:
                        raise SystemExit(f"review-repair-claims-changed:{role}")
        message = completions[0]["last_agent_message"].strip()
        if output_path.read_text(encoding="utf-8").strip() != message:
            raise SystemExit(f"provenance-output-message-mismatch:{role}")
        expected_header = (
            f"<!-- codex-review-provenance role={role} run={manifest['review_run_id']} "
            f"input={manifest['review_input_sha256']} context={attempt['context_sha256']} "
            f"attempt={attempt['attempt']} -->"
        )
        if manifest.get("schema_version") not in {7, 8} and message.splitlines()[0] != expected_header:
            raise SystemExit(f"provenance-output-header-mismatch:{role}")

    if attempts[selected - 1].get("status") != "completed":
        raise SystemExit(f"manifest-selected-attempt-not-completed:{role}")
    canonical_output = _resolve_path(out_dir, item.get("output_path"))
    selected_output = _resolve_path(out_dir, attempts[selected - 1].get("output_path"))
    if canonical_output != selected_output:
        raise SystemExit(f"manifest-selected-output-mismatch:{role}")


def _batch_reviewer_findings(
    path: Path,
    snapshot: dict[str, Any],
    role: str,
    *,
    content_override: str | None = None,
    expected_provenance_header: str | None = None,
) -> list[dict[str, Any]]:
    """Parse explicit batched obligations and bind declared coordinates to frozen source hashes.

    Ordinary native and historical reviewer formats never enter this profile. Missing inventories and prose outside the
    allowed sections fail; ratings and blocking counts cannot invent or suppress records. A current native caller may
    supply its frozen marker for a read-only leading-prefix view; standalone and repair callers stay strict.
    """
    content = path.read_text(encoding="utf-8") if content_override is None else content_override
    if expected_provenance_header is not None:
        content = content.removeprefix(expected_provenance_header + "\n")
    match = re.fullmatch(
        r"\s*## Reviewer Findings\s*\n```json\n(?P<records>.*?)\n```\s*"
        r"(?:## Finding Dispositions\s*\n(?P<dispositions>.*?))?"
        r"## Reviewer Confidence\s*\n```json\n(?P<confidence>.*?)\n```\s*"
        r"## Reviewer Assessment\s*\n(?P<assessment>.*?)\s*",
        content,
        re.DOTALL,
    )
    if match is None or _text_reviewer_assessment(match["assessment"], batch_response=True) is None:
        raise SystemExit(f"review-batch-individual-findings-format:{role}")
    dispositions = match["dispositions"]
    if dispositions is not None and any(
        re.fullmatch(
            r"Source disposition [^\s:]+: (closed|rejected); Evidence: .+:[1-9][0-9]*-[1-9][0-9]* - \S.*", line
        )
        is None
        for line in dispositions.strip().splitlines()
    ):
        raise SystemExit(f"review-batch-individual-findings-disposition-format:{role}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Reject duplicate JSON keys rather than silently replacing an obligation field."""
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate finding key")
            result[key] = value
        return result

    try:
        records = json.loads(match["records"], object_pairs_hook=unique_object)
        confidence = json.loads(match["confidence"], object_pairs_hook=unique_object)
    except (ValueError, RecursionError) as error:
        raise SystemExit(f"review-batch-individual-findings-json:{role}") from error
    if (
        not isinstance(confidence, dict)
        or set(confidence) != {"score", "scope", "gaps"}
        or type(confidence["score"]) not in {int, float}
        or not 0 <= confidence["score"] <= 1
        or not isinstance(confidence["scope"], str)
        or not confidence["scope"].strip()
        or not isinstance(confidence["gaps"], list)
        or any(
            not isinstance(gap, dict)
            or set(gap) != {"gap", "status", "rationale"}
            or any(not isinstance(gap[key], str) or not gap[key].strip() for key in gap)
            or gap["status"] not in {"closed", "unresolved", "deferred"}
            for gap in confidence["gaps"]
        )
    ):
        raise SystemExit(f"review-batch-individual-findings-confidence:{role}")
    if not isinstance(records, list):
        raise SystemExit(f"review-batch-individual-findings-inventory:{role}")
    sources = {item["path"]: item for item in snapshot["files"]}
    identities = set()
    bound = []
    fields = {"id", "severity", "title", "summary", "required_change", "evidence", "closure_evidence"}
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != fields
            or not isinstance(record["id"], str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", record["id"]) is None
            or record["id"] in identities
            or not isinstance(record["severity"], str)
            or record["severity"] not in FINDING_SEVERITIES
            or any(not isinstance(record[key], str) or not record[key].strip() for key in fields - {"evidence"})
            or not isinstance(record["evidence"], list)
        ):
            raise SystemExit(f"review-batch-individual-findings-record:{role}")
        identities.add(record["id"])
        evidence = []
        for entry in record["evidence"]:
            if not isinstance(entry, dict) or set(entry) != {"path", "start_line", "end_line"}:
                raise SystemExit(f"review-batch-individual-findings-evidence:{role}")
            source = sources.get(entry["path"]) if isinstance(entry["path"], str) else None
            if (
                source is None
                or source["kind"] == "missing"
                or type(entry["start_line"]) is not int
                or type(entry["end_line"]) is not int
                or not 1 <= entry["start_line"] <= entry["end_line"] <= len(source["content"].splitlines())
            ):
                raise SystemExit(f"review-batch-individual-findings-evidence:{role}")
            evidence.append({**entry, "source_sha256": source["sha256"]})
        bound.append({**record, "evidence": evidence})
    return bound
