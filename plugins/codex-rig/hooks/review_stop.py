#!/usr/bin/env python3
"""Request one diagnostic continuation when review or remediation stops before closure.

## Purpose

Catch an observed failure mode where the parent starts review or remediation, leaves authorized stages unfinished, and
emits a final answer before the workflow's completion check. Instructions and artifact validators cannot intercept an
answer that bypasses those validators; the host Stop event supplies that interception point.

## Scope

Read only the bounded host transcript supplied with the Stop event. Bind successful direct packaged helper executions to
the current session and turn, including run promotion and same-run completion. This is an advisory recovery prompt, not
an authorization or security boundary. Unknown lifecycle shapes, visible controlling stops and already-continued turns
produce no continuation. Transcript compatibility is deliberately narrow because the host format is unstable.

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
    for record in records:
        data = record.get("payload", {})
        if not isinstance(data, dict):
            return {}
        kind = data.get("type")
        if record.get("type") == "event_msg" and kind == "task_started":
            active = data.get("turn_id") == turn
            runs.clear()
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
                call = str(data.get("name", "")) + str(data.get("input", data.get("arguments", "")))
                if QUESTION.search(call):
                    return {}
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
            continue
        args = _argv(item, plugin_root)
        if not args:
            continue
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
            if len(args) < 4 or args[2] != "--skill" or args[3] not in {"code-review", "code-remediate"}:
                continue
            if len(args) not in {4, 6} or (len(args) == 6 and args[4] != "--promote-pr-run"):
                continue
            # Some host CommandExecution records retain escaped terminal newlines.
            report = output.strip().removesuffix("\\n")
            if not report or "\n" in report or "\r" in report:
                return {}
            run = _coordinate(report, cwd)
            skill = args[3]
            root = _coordinate(f".reports/codex/{skill}", cwd) + "/"
            if not run.startswith(root):
                return {}
            if len(args) == 6:
                previous = _coordinate(args[5], cwd)
                if runs.get(previous) != skill:
                    return {}
                del runs[previous]
            runs[run] = skill
        elif helper == _coordinate("shared/find-review-report.py", plugin_root):
            if len(args) == 4 and args[2] == "--complete-run":
                run = _coordinate(args[3], cwd)
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
    if not active or not runs:
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
            "Prepare eligible dependencies and continue independent inspection despite failed tests. Do not end "
            "with a list of work you can already perform or ask the user to repeat the request."
        ),
    }


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
        with Path(transcript).open("rb") as stream:
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
