#!/usr/bin/env python3
"""Request one diagnostic continuation when review or remediation stops before closure.

## Purpose

Catch an observed failure mode where the parent starts review or remediation, leaves authorized stages unfinished, and
emits a final answer before the workflow's completion check. Instructions and artifact validators cannot intercept an
answer that bypasses those validators; the host Stop event supplies that interception point.

## Scope

Read only the bounded host transcript supplied with the Stop event. Bind successful direct packaged helper executions to
the current session and turn, including run promotion and same-run completion. Creation may precede a bounded literal
read-only suffix batch; completion requires its own successful direct command. This is an advisory recovery prompt, not
an authorization or security boundary. Unknown lifecycle shapes, visible controlling stops and already-continued turns
produce no continuation. Known native Continue answers resolve only outstanding question invocations with distinct host
receipts and matching decision/scope identities; discovery text is not a question. Occurrence accounting does not prove
exact outer JavaScript call correlation. Transcript compatibility is deliberately narrow because the host format is
unstable.

Pause recognition also searches the assistant's own final prose, so it is model-controllable: ordinary or injected words
such as ``awaiting`` or ``denied`` can suppress continuation without host evidence of a stop. This conservative tradeoff
avoids unwanted turns but means the advisory cannot enforce completion against model-controlled text.

## Usage

The plugin's Stop hook invokes this file with the host event on stdin and PLUGIN_ROOT in its environment. The host must
trust the hook definition before it runs. Local replay can supply the same envelope and a retained transcript prefix.

## Outputs

Print one JSON hook response. A proved unfinished active workflow requests one continuation to inspect stops first and
finish only already-authorized work; other inputs return an empty object. No transcript content is copied to output.

## Failure

Malformed, oversized, unavailable or unsupported input fails open. No subprocess, network, installation, permission
change or filesystem write occurs. A one-shot continuation does not guarantee that a model completes the review.

## Used by

The bundled hooks/hooks.json Stop handler and portable hook regression tests. The existing review completion checker
still owns validation; this hook neither accepts a result nor creates review evidence.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlsplit

MAX_INPUT_BYTES = 65_536
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
CONTROLLING_STOP = re.compile(
    r"denied|declined|rejected by|awaiting|waiting for|need your|requires? your|"
    r"pending (?:user |scope )?(?:decision|question|approval)|"
    r"(?:retry|attempt) (?:limit|budget).{0,30}exhaust|"
    r"(?:closed|merged) (?:PR|pull request)|(?:PR|pull request) is (?:closed|merged)|"
    r"(?:unverified|unavailable|missing) source|source identity is (?:unverified|unavailable)|"
    r"user.{0,20}(?:stop|pause|cancel)",
    re.IGNORECASE,
)
QUESTION = re.compile(r"request_user_input|ask_user|interrupt_agent")
NESTED_QUESTION = re.compile(
    r"\btools\s*\.\s*(\w*(?:request_user_input(?:_async)?|ask_user|interrupt_agent))\b(\s*\()?"
)
NATIVE_QUESTION = "mcp__codex_rig_input__ask_user"
QUESTION_DISCOVERY = re.compile(
    r"\s*text\(ALL_TOOLS\.filter\(([A-Za-z_]\w*)\s*=>\s*/[A-Za-z_|]+/\.test\(\1\.name\)\)\);?\s*"
)


def _resolved_question(item: dict) -> bool:
    """Recognize a bound Continue answer or the known scope error raised before opening a form."""
    if (
        item.get("server") != "codex-rig-input"
        or item.get("tool") != "ask_user"
        or item.get("pluginId") != "codex-rig@borda-ai-rig"
    ):
        return False
    arguments = item.get("arguments")
    if not isinstance(arguments, dict):
        return False
    scope = arguments.get("scope_digest")
    valid_scope = isinstance(scope, str) and re.fullmatch(r"[0-9a-f]{64}", scope) is not None
    if item.get("status") == "failed":
        # This specific validation precedes form creation. Other failures can leave
        # a decision pending, including the same error code for a conflicting ID.
        error = item.get("error")
        return (
            not valid_scope
            and isinstance(error, dict)
            and isinstance(error.get("message"), str)
            and error["message"].endswith("Mcp error: -32602: scope_digest must be 64 lowercase hexadecimal characters")
        )
    identity = arguments.get("decision_id")
    options = arguments.get("options")
    result = item.get("result")
    if (
        item.get("status") != "completed"
        or not valid_scope
        or not isinstance(identity, str)
        or not identity.strip()
        or len(identity) > 128
        or not isinstance(options, list)
        or "Continue" not in options
        or not isinstance(result, dict)
        or result.get("isError", False) is not False
    ):
        return False
    return result.get("structuredContent") == {
        "decision_id": identity,
        "scope_digest": scope,
        "status": "answered",
        "answer": "Continue",
    }


def _coordinate(value: str, cwd: str) -> str:
    """Compare serialized host paths without applying the hook machine's path rules."""
    cls = PureWindowsPath if PureWindowsPath(cwd).drive else PurePosixPath
    path = cls(value)
    if ".." in path.parts:
        raise ValueError("parent traversal")
    if not path.is_absolute():
        path = cls(cwd) / path
    return path.as_posix().casefold() if cls is PureWindowsPath else path.as_posix()


def _argv(item: dict, plugin_root: str) -> list[str]:
    """Recognize only direct packaged launcher calls in known host shell envelopes."""
    command = item.get("command")
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        return []
    if len(command) != 3:
        return []
    shell = command[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    windows = shell in {"pwsh", "pwsh.exe", "powershell", "powershell.exe"}
    if (windows and command[1].lower() != "-command") or (
        not windows and (shell not in {"bash", "sh", "zsh"} or command[1] not in {"-c", "-lc"})
    ):
        return []
    source = command[2].strip()
    if windows and source.startswith("& "):
        source = source[2:]
    # Only a literal direct call is understood; shell evaluation is never attempted.
    if any(character in source for character in ";&|<>`$\n\r"):
        return []
    if windows:
        # Keep a quoted PowerShell option value attached to its equals-form key
        # when shlex applies its deliberately narrow non-POSIX tokenization.
        source = re.sub(
            r"(?<!\S)(--(?:complete-run|codex-home|parent-thread-id|pr-number|branch|skill|promote-pr-run))=([\"'])(.*?)\2(?=\s|$)",
            r"\2\1=\3\2",
            source,
        )
    try:
        args = shlex.split(source, posix=not windows)
    except ValueError:
        return []
    if windows:
        # PowerShell supports either literal quote form; remove only a matched pair,
        # without evaluating expressions or unescaping the path.
        args = [
            part[1:-1] if len(part) >= 2 and part[0] in {'"', "'"} and part[-1] == part[0] else part for part in args
        ]
    if len(args) < 2:
        return []
    path_type = PureWindowsPath if windows else PurePosixPath
    if not all(path_type(argument).is_absolute() for argument in args[:2]):
        return []
    launcher = "bin/python.cmd" if windows else "bin/python"
    if _coordinate(args[0], plugin_root) != _coordinate(launcher, plugin_root):
        return []
    return args


def _creation_batch(item: dict, plugin_root: str) -> tuple[list[str], str]:
    """Read the creator's first path receipt before a bounded sequence of literal read-only commands."""
    command = item.get("command")
    output = item.get("aggregated_output")
    if not isinstance(command, list) or len(command) != 3 or not isinstance(command[2], str):
        return [], ""
    lines = command[2].splitlines()
    if len(lines) < 2 or not isinstance(output, str) or not output.splitlines():
        return [], ""
    first = _argv({"command": [*command[:2], lines[0]]}, plugin_root)
    if not first or _coordinate(first[1], plugin_root) != _coordinate("shared/create_run.py", plugin_root):
        return [], ""
    for line in lines[1:]:
        if line.strip() in {"rtk git status --short", "rtk git rev-parse HEAD"}:
            continue
        tail = _argv({"command": [*command[:2], line]}, plugin_root)
        if (
            len(tail) != 4
            or _coordinate(tail[1], plugin_root) != _coordinate("shared/find-review-report.py", plugin_root)
            or tail[2] != "--result"
            or not tail[3]
            or tail[3].startswith("-")
        ):
            return [], ""
    # The batch exit code belongs to its tail. Only creation has a path-only
    # success receipt; never use this route to infer completion of a finalizer.
    return first, output.splitlines()[0]


def evaluate(payload: dict, records: list[dict], plugin_root: str) -> dict:
    """Continue positively bound review or remediation work without overriding a controlling pause."""
    if payload.get("hook_event_name") != "Stop" or payload.get("stop_hook_active") is not False:
        return {}
    session, turn, cwd = (payload.get(name) for name in ("session_id", "turn_id", "cwd"))
    message = payload.get("last_assistant_message")
    if not all(isinstance(value, str) and value for value in (session, turn, cwd, message, plugin_root)):
        return {}
    if CONTROLLING_STOP.search(message):
        return {}
    if not records or records[0].get("type") != "session_meta" or records[0].get("payload", {}).get("id") != session:
        return {}
    active = False
    runs: dict[str, str] = {}
    pending_questions = 0
    question_receipts: set[str] = set()
    for record in records:
        data = record.get("payload", {})
        if not isinstance(data, dict):
            return {}
        kind = data.get("type")
        if record.get("type") == "event_msg" and kind == "task_started":
            active = data.get("turn_id") == turn
            runs.clear()
            pending_questions = 0
            question_receipts.clear()
            continue
        if not active:
            continue
        if (
            record.get("type") == "inter_agent_communication_metadata"
            and set(data) == {"trigger_turn"}
            and data["trigger_turn"] is False
        ):
            continue
        if record.get("type") not in {
            "event_msg",
            "response_item",
            "turn_context",
            "world_state",
            "token_usage_record",
        }:
            return {}
        if record.get("type") == "response_item":
            if kind == "configuration_update":
                # The host emits an effort-only update before ordinary commands. It changes
                # reasoning settings, not authorization or whether the user stopped work.
                if (
                    set(data) != {"type", "reasoning"}
                    or not isinstance(data["reasoning"], dict)
                    or set(data["reasoning"]) != {"effort"}
                    or not isinstance(data["reasoning"]["effort"], str)
                    or data["reasoning"]["effort"] not in {"low", "medium", "high", "xhigh", "max", "ultra"}
                ):
                    return {}
            elif kind in {"function_call", "custom_tool_call"}:
                name = str(data.get("name", ""))
                if name == NATIVE_QUESTION:
                    pending_questions += 1
                elif QUESTION.search(name):
                    return {}
                else:
                    # Mentions in tool discovery are not invocations. Recognize only
                    # the literal nested tool route; aliases remain unsupported.
                    source = str(data.get("input", data.get("arguments", "")))
                    # Only this inert name-filter expression is known discovery. All
                    # other unmatched question references retain the conservative pause,
                    # including bracket calls, destructuring and optional chaining.
                    if name == "exec" and QUESTION_DISCOVERY.fullmatch(source):
                        continue
                    if QUESTION.search(NESTED_QUESTION.sub("", source)):
                        return {}
                    for question in NESTED_QUESTION.finditer(source):
                        if question[1] != NATIVE_QUESTION or not question[2] or name != "exec":
                            return {}
                        pending_questions += 1
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                output = data.get("output", "")
                # Host rejections can have no CommandExecution receipt. Do not retry them.
                if re.search(
                    r"(?:(?:denied|rejected) by (?:the )?user|user denied|approval denied|auto.?review.{0,40}reject)",
                    str(output),
                    re.I,
                ):
                    return {}
            elif kind == "message" and data.get("role") == "user" and runs:
                return {}
            elif kind not in {"message", "reasoning", "additional_tools", "agent_message"}:
                return {}
            continue
        if record.get("type") != "event_msg":
            continue
        if kind in {"token_count", "thread_settings_applied", "agent_message"}:
            continue
        if kind == "task_complete" and data.get("turn_id") == turn:
            continue
        if kind != "item_completed":
            return {}
        if data.get("turn_id") != turn or data.get("thread_id") != session:
            return {}
        item = data.get("item", {})
        item_type = item.get("type")
        if item_type == "UserMessage":
            if runs:
                return {}
            continue
        if item_type in {"Reasoning", "AgentMessage"}:
            continue
        if item_type == "SubAgentActivity":
            if item.get("kind") not in {"started", "completed"}:
                return {}
            continue
        if item_type == "CollabAgentToolCall":
            if item.get("tool") not in {"spawn", "wait", "send_input", "resume"}:
                return {}
            continue
        if item_type == "McpToolCall":
            receipt_id = item.get("id")
            if (
                pending_questions <= 0
                or not isinstance(receipt_id, str)
                or not receipt_id
                or receipt_id in question_receipts
                or not _resolved_question(item)
            ):
                return {}
            question_receipts.add(receipt_id)
            pending_questions -= 1
            continue
        if item_type != "CommandExecution":
            return {}
        if item.get("status") not in {"completed", "failed"} or type(item.get("exit_code")) is not int:
            return {}
        output = item.get("aggregated_output", "")
        if not isinstance(output, str):
            return {}
        if item.get("exit_code") != 0:
            if re.search(r"denied|not permitted|approval|retry.{0,30}exhaust", output, re.I):
                return {}
        args = _argv(item, plugin_root) if item.get("exit_code") == 0 else []
        batched_creation = False
        if not args:
            args, output = _creation_batch(item, plugin_root)
            if not args:
                continue
            batched_creation = True
        command_cwd = item.get("cwd")
        if not isinstance(command_cwd, str):
            return {}
        if command_cwd.startswith("file://"):
            uri = urlsplit(command_cwd)
            if uri.netloc:
                return {}
            command_cwd = unquote(uri.path)
            if re.match(r"^/[A-Za-z]:/", command_cwd):
                command_cwd = command_cwd[1:]
        if _coordinate(command_cwd, cwd) != _coordinate(cwd, cwd):
            continue
        helper = _coordinate(args[1], plugin_root)
        if helper == _coordinate("shared/create_run.py", plugin_root):
            creation: dict[str, str] = {}
            arguments = iter(args[2:])
            valid_creation = False
            for argument in arguments:
                option, separator, value = argument.partition("=")
                if option not in {"--skill", "--pr-number", "--branch", "--promote-pr-run"} or option in creation:
                    break
                if not separator:
                    value = next(arguments, "")
                if not value.strip() or value.startswith("-"):
                    break
                creation[option] = value
            else:
                skill = creation.get("--skill", "")
                identity_options = set(creation) - {"--skill"}
                if skill not in {"code-review", "code-remediate"} or len(identity_options) > 1:
                    continue
                number = creation.get("--pr-number")
                if number is not None and (not number.isascii() or not number.isdecimal() or int(number) <= 0):
                    continue
                # Promotion and creation are separate calls; duplicate or mixed-mode options cannot bind a run.
                valid_creation = True
            if not valid_creation:
                continue
            # Some host CommandExecution records retain escaped terminal newlines.
            report = output.strip().removesuffix("\\n")
            if not report or "\n" in report or "\r" in report:
                return {}
            run = _coordinate(report, cwd)
            root = _coordinate(f".reports/codex/{skill}", cwd) + "/"
            if not run.startswith(root):
                return {}
            if batched_creation:
                relative = run.removeprefix(root)
                if "--promote-pr-run" in creation:
                    if re.fullmatch(r"pr-[1-9][0-9]*/run-[0-9]{3,}", relative) is None:
                        return {}
                elif "/" in relative or relative.endswith(".json"):
                    return {}
            if "--promote-pr-run" in creation:
                previous = _coordinate(creation["--promote-pr-run"], cwd)
                if runs.get(previous) != skill:
                    return {}
                del runs[previous]
            runs[run] = skill
        elif helper == _coordinate("shared/find-review-report.py", plugin_root):
            # Context options do not change which run completed. Parse only known
            # valued options so an exit-zero --help call cannot clear active work.
            completion: dict[str, str] = {}
            arguments = iter(args[2:])
            for argument in arguments:
                option, separator, value = argument.partition("=")
                if option not in {"--complete-run", "--codex-home", "--parent-thread-id"} or option in completion:
                    break
                if not separator:
                    value = next(arguments, "")
                if not value or value.startswith("-"):
                    break
                completion[option] = value
            else:
                if "--complete-run" in completion:
                    run = _coordinate(completion["--complete-run"], cwd)
                    if runs.get(run) == "code-review":
                        del runs[run]
        elif helper == _coordinate("shared/remediation_finalize.py", plugin_root):
            if args[2:3] != ["finalize"] or "--promote" not in args or args.count("--run") != 1:
                continue
            index = args.index("--run") + 1
            if index >= len(args):
                return {}
            run = _coordinate(args[index], cwd)
            # The existing finalizer exits zero only after validation and requested
            # promotion. Its default skill is remediation; review promotion stays separate.
            if "--skill" in args:
                index = args.index("--skill") + 1
                if args.count("--skill") != 1 or index >= len(args) or args[index] != "code-remediate":
                    continue
            if runs.get(run) == "code-remediate":
                del runs[run]
    if not active or not runs or pending_questions:
        return {}
    return {
        "decision": "block",
        "reason": (
            "Codex Rig detected that this turn started review or remediation without its completion check. "
            "This is a one-time workflow diagnostic, not new authorization. First honor any user stop, denial, "
            "pending question, closed PR, unverified source or exhausted retry limit; if one applies, preserve "
            "that pause and ask the concrete missing decision without retrying a denied action. Otherwise execute "
            "the first unmet checkpoint now under existing authority. For +review, complete the needed review, "
            "admit its exact result, then return to remediation selection, selected fixes, verification and delivery. "
            "A missing selection requires its normal user question; an unstarted step does not require permission. "
            "Prepare eligible dependencies and continue independent inspection despite failed tests. Read-only review "
            "does not waive configured checks: use disposable source copies for autofixing hooks. A missed blind "
            "checkpoint stays disclosed; it does not replace an actual finalization attempt. Do not end "
            "with a list of work you can already perform or ask the user to repeat the request."
        ),
    }


def _open_transcript(path: str, flags: int) -> int:
    """Open a regular transcript without waiting for a FIFO writer, including after a path replacement."""
    if not Path(path).is_file():
        raise ValueError("transcript is not a regular file")
    descriptor = os.open(path, flags | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("transcript is not a regular file")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def main() -> int:
    """Read bounded host evidence and always emit valid nonblocking or continuation JSON."""
    response: dict = {}
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("oversized event")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("invalid event")
        transcript = payload.get("transcript_path")
        if not isinstance(transcript, str):
            raise ValueError("missing transcript")
        with open(transcript, "rb", opener=_open_transcript) as stream:
            before = os.fstat(stream.fileno())
            size = before.st_size
            if size <= MAX_TRANSCRIPT_BYTES:
                raw = stream.read(MAX_TRANSCRIPT_BYTES + 1)
                if len(raw) != size:
                    raise ValueError("transcript changed")
                records = [json.loads(line) for line in raw.splitlines() if line.strip()]
            else:
                # Preserve the session identity and a bounded complete suffix; a missing turn start fails open.
                first = stream.readline(MAX_TRANSCRIPT_BYTES + 1)
                if len(first) >= MAX_TRANSCRIPT_BYTES or not first.endswith(b"\n"):
                    raise ValueError("invalid session metadata")
                budget = MAX_TRANSCRIPT_BYTES - len(first)
                stream.seek(size - budget - 1)
                raw = stream.read(budget + 1)
                if len(raw) != budget + 1 or b"\n" not in raw or not raw.endswith(b"\n"):
                    raise ValueError("incomplete transcript suffix")
                raw = raw.partition(b"\n")[2]
                records = [json.loads(first), *[json.loads(line) for line in raw.splitlines() if line.strip()]]
            after = os.fstat(stream.fileno())
            if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (size, before.st_mtime_ns, before.st_ctime_ns):
                raise ValueError("transcript changed")
        if not all(isinstance(record, dict) for record in records):
            raise ValueError("invalid transcript")
        response = evaluate(payload, records, os.environ.get("PLUGIN_ROOT", ""))
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
        pass
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
