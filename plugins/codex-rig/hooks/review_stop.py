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
produce no continuation. Known native continuation and review-entry answers resolve only outstanding questions with
distinct host receipts and matching decision/scope identities. Checkout-slot receipts can bind recovery before run
creation and supersede an earlier observation at the same slot. Recovery ends for the turn on a terminal plain-text
question before run creation, a user message after any validated slot receipt, or a helper error at a slot the turn
already admitted. Receipt routing reads receipt fields; pause words inside owner IDs or slot paths are data. Every
command receipt accepts the same host output endings. Occurrence accounting does not prove exact outer JavaScript call
correlation. Transcript compatibility is deliberately narrow because the host format is unstable.

Pause recognition also searches the assistant's own final prose, so it is model-controllable: ordinary or injected words
such as ``awaiting`` or ``denied`` can suppress continuation without host evidence of a stop. Ordinary checkout wait
wording yields to current peer evidence. The advisory cannot enforce completion against model-controlled text.

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
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

MAX_INPUT_BYTES = 65_536
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
CONTROLLING_STOP = re.compile(
    r"denied|declined|rejected by|awaiting|waiting for|need your|requires? your|"
    r"pending (?:user |scope )?(?:decision|question|approval)|"
    r"(?:your|user) (?:decision|answer|approval) is pending|"
    r"(?:retry|attempt) (?:limit|budget).{0,30}exhaust|"
    r"(?:closed|merged) (?:PR|pull request)|(?:PR|pull request) is (?:closed|merged)|"
    r"(?:unverified|unavailable|missing) source|source identity is (?:unverified|unavailable)|"
    r"user.{0,20}(?:stop|pause|cancel)",
    re.IGNORECASE,
)
CHECKOUT_WAIT = re.compile(
    r"\b(?:awaiting|waiting for) (?:the )?(?:(?:shared )?checkout (?:slot|admission)|other checkout owner)\b",
    re.IGNORECASE,
)
DECISION_WAIT = re.compile(
    r"\b(?:(?:awaits?|awaiting|waiting for) (?:your|the) (?:decision|answer)|"
    r"waiting for your approval before running the command|pending user decision)\b",
    re.IGNORECASE,
)
REPLY_INSTRUCTION = re.compile(r"(?:^|[?!.]\s+)(?:please\s+)?(?:reply|choose|select|enter)\s+\S", re.IGNORECASE | re.M)
ACTION_LABEL = r"approve|deny|continue|skip|stop|resume last(?: one)?|start fresh|new review"
CHOICE_LINE = re.compile(rf"^\s*(?:[-*]|\d+[.)])\s+[`*]*({ACTION_LABEL})\b", re.IGNORECASE | re.M)
INLINE_CHOICES = re.compile(rf"^[`* ]*(?:{ACTION_LABEL})[`* ]*(?:/|\bor\b)[`* ]*(?:{ACTION_LABEL})\b", re.IGNORECASE)
QUESTION = re.compile(r"request_user_input|ask_user|interrupt_agent")
NESTED_QUESTION = re.compile(
    r"\btools\s*\.\s*(\w*(?:request_user_input(?:_async)?|ask_user|interrupt_agent))\b(\s*\()?"
)
NATIVE_QUESTION = "mcp__codex_rig_input__ask_user"
QUESTION_DISCOVERY = re.compile(
    r"\s*text\(ALL_TOOLS\.filter\(([A-Za-z_]\w*)\s*=>\s*/[A-Za-z_|]+/\.test\(\1\.name\)\)\);?\s*"
)
SLOT_OWNER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
# A final block ending in a question mark, optionally followed by closing emphasis, code or quote marks.
TERMINAL_QUESTION = re.compile(r"\?[`*_\"')\]]*$")
# Every continuation reason carries the same pause precedence, so no route answers a pending question.
CONTROLLING_PAUSE = (
    "First honor any user stop, denial, pending question, closed PR, unverified source or exhausted retry limit; "
    "if one applies, preserve that pause and ask the concrete missing decision without retrying a denied action."
)


class CheckoutAdmission(NamedTuple):
    """Retain the requester, locator and latest controlling observation for one admission."""

    requester: str
    slot: str
    state: str
    holder: str | None
    via_checkout: bool


class CheckoutSlotReceipt(NamedTuple):
    """Represent one validated operation on a locator or its bound checkout."""

    action: str
    requester: str | None
    slot: str | None
    status: str
    holder: str | None
    via_checkout: bool


def _resolved_question(item: dict) -> bool:
    """Recognize a bound workflow continuation answer or the known scope error raised before opening a form."""
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
        or not isinstance(result, dict)
        or result.get("isError", False) is not False
    ):
        return False
    submitted = result.get("structuredContent")
    if not isinstance(submitted, dict):
        return False
    answer = submitted.get("answer")
    if not (
        (answer == "Continue" and answer in options)
        or (options == ["Resume last", "Start fresh"] and answer in options)
    ):
        return False
    return submitted == {
        "decision_id": identity,
        "scope_digest": scope,
        "status": "answered",
        "answer": answer,
    }


def _host_output(output: str) -> str:
    """Normalize one command record's text across the output endings hosts are known to record.

    A host records a real terminal newline, no terminator, or the escaped two-character ``\\n`` that some
    CommandExecution records retain. Only that terminal marker is removed. An escaped sequence elsewhere stays data,
    because a JSON receipt can carry one inside a string value such as an error message.
    """
    return output.strip().removesuffix("\\n")


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
            r"(?<!\S)(--(?:complete-run|codex-home|parent-thread-id|pr-number|pr-url|branch|skill|local-scope|local-target|promote-pr-run))=([\"'])(.*?)\2(?=\s|$)",
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
        if line.strip().removeprefix("rtk ") in {"git status", "git status --short", "git rev-parse HEAD"}:
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


def _checkout_slot_receipt(args: list[str], output: str, exit_code: int, cwd: str) -> CheckoutSlotReceipt | None:
    """Validate the packaged helper's invocation, coordinates, exit status and receipt fields together."""
    requester = None
    wait: list[str] = []
    match args:
        case ["acquire" as action, ("--checkout" | "--slot") as flag, target, "--owner", requester, *wait]:
            if wait not in ([], ["--queue"]) and (
                len(wait) != 2
                or wait[0] != "--wait-seconds"
                or not re.fullmatch(r"[0-9]{1,2}", wait[1])
                or int(wait[1]) > 60
            ):
                return None
        case ["status" as action, ("--checkout" | "--slot") as flag, target]:
            pass
        case [("hold" | "release") as action, "--slot" as flag, target, "--owner", requester]:
            pass
        case ["release" as action, "--inspected", "--slot" as flag, target, "--owner", requester] | [
            "release" as action,
            "--slot" as flag,
            target,
            "--owner",
            requester,
            "--inspected",
        ]:
            pass
        case _:
            return None
    if requester is not None and not SLOT_OWNER.fullmatch(requester):
        return None

    path_type = PureWindowsPath if PureWindowsPath(cwd).drive else PurePosixPath
    if not path_type(target).is_absolute():
        return None
    via_checkout = flag == "--checkout"
    try:
        output = _host_output(output)
        if wait == ["--queue"]:
            # The host combines peer-change progress from stderr with the final stdout record.
            output = output.rsplit("\n", 1)[-1]
        receipt = json.loads(output)
        requested = _coordinate(target, cwd)
        if via_checkout and requested != _coordinate(cwd, cwd):
            return None
        fields = {"status", "slot", "owner"}
        if exit_code == 2:
            fields.add("error")
        if not isinstance(receipt, dict) or set(receipt) != fields:
            return None
        slot, status, holder = receipt["slot"], receipt["status"], receipt["owner"]
        error = exit_code == 2 and status == "error" and holder is None and isinstance(receipt["error"], str)
        if slot is None and error and via_checkout:
            return CheckoutSlotReceipt(action, requester, None, status, holder, via_checkout)
        if (
            not isinstance(slot, str)
            or not path_type(slot).is_absolute()
            or path_type(slot).name != "codex-review-slot"
        ):
            return None
        slot = _coordinate(slot, cwd)
        if not via_checkout and requested != slot:
            return None
    except ValueError:
        return None

    if holder is not None and (not isinstance(holder, str) or not SLOT_OWNER.fullmatch(holder)):
        return None
    if error:
        return CheckoutSlotReceipt(action, requester, slot, status, holder, via_checkout)
    observation = status == "busy" or (status == "inspection-required" and holder is not None)
    if (
        (action == "acquire" and exit_code == 0 and status == "acquired" and holder == requester)
        or (action == "acquire" and exit_code == 1 and observation)
        or (action == "status" and exit_code == 0 and (observation or (status == "free" and holder is None)))
        or (action == "hold" and exit_code == 0 and status == "inspection-required" and holder == requester)
        or (action == "release" and exit_code == 0 and status == "released" and holder is None)
        or (action in {"hold", "release"} and exit_code == 1 and status == "not-owner" and holder != requester)
        or (
            action == "release"
            and exit_code == 1
            and status == "inspection-required"
            and holder == requester
            and "--inspected" not in args
        )
    ):
        return CheckoutSlotReceipt(action, requester, slot, status, holder, via_checkout)
    return None


def _checkout_admission(previous: CheckoutAdmission | None, receipt: CheckoutSlotReceipt) -> CheckoutAdmission | None:
    """Update admission only from the same locator and an applicable requester or holder."""
    action, requester, slot, status, holder, via_checkout = receipt
    if slot is None:
        # The outer checkout binding judges a failure before locator resolution.
        return previous
    if action == "acquire":
        if requester is None or (previous is not None and (requester, slot) != (previous.requester, previous.slot)):
            return previous
        via_checkout = via_checkout or (previous is not None and previous.via_checkout)
        if status in {"acquired", "error"}:
            return CheckoutAdmission(requester, slot, status, holder, via_checkout)
        if status == "inspection-required" and holder == requester:
            return CheckoutAdmission(requester, slot, "stop", holder, via_checkout)
        if previous is not None and holder == requester and previous.state in {"acquired", "stop"}:
            return previous._replace(via_checkout=via_checkout)
        return (
            CheckoutAdmission(requester, slot, "peer", holder, via_checkout)
            if holder not in (None, requester)
            else None
        )
    if previous is None or slot != previous.slot or requester not in (None, previous.requester, previous.holder):
        return previous
    via_checkout = previous.via_checkout or via_checkout
    if status == "error" or (status == "inspection-required" and holder == previous.requester):
        return previous._replace(state="stop", holder=holder, via_checkout=via_checkout)
    if status in {"busy", "inspection-required"}:
        if previous.state == "acquired" and holder == previous.requester:
            return previous._replace(via_checkout=via_checkout)
        if previous.state == "peer" and holder not in (None, previous.requester):
            return previous._replace(holder=holder, via_checkout=via_checkout)
    return None


def evaluate(payload: dict, records: list[dict], plugin_root: str) -> dict:
    """Continue positively bound review or remediation work without overriding a controlling pause."""
    if payload.get("hook_event_name") != "Stop" or payload.get("stop_hook_active") is not False:
        return {}
    session, turn, cwd = (payload.get(name) for name in ("session_id", "turn_id", "cwd"))
    message = payload.get("last_assistant_message")
    if not all(isinstance(value, str) and value for value in (session, turn, cwd, message, plugin_root)):
        return {}
    delivery_only = DECISION_WAIT.search(message) is not None
    stop_prose = DECISION_WAIT.sub("", message)
    # A typed fallback can end a turn independently of any wait-status wording.
    # Inspect the terminal block so earlier evidence and ordinary lists of checks do not become a menu.
    terminal_block = message.rstrip().rsplit("\n\n", 1)[-1]
    if (
        CONTROLLING_STOP.search(CHECKOUT_WAIT.sub("", stop_prose))
        or REPLY_INSTRUCTION.search(terminal_block)
        or INLINE_CHOICES.match(terminal_block)
        or len(set(CHOICE_LINE.findall(terminal_block.lower()))) >= 2
    ):
        return {}
    if not records or records[0].get("type") != "session_meta" or records[0].get("payload", {}).get("id") != session:
        return {}
    active = False
    runs: dict[str, str] = {}
    owner = ""
    pending_questions = 0
    question_receipts: set[str] = set()
    async_call = ""
    async_state = ""
    checkout: CheckoutAdmission | None = None
    checkout_binding: CheckoutAdmission | None = None
    # Turn-scoped slot evidence survives an admission reset, so a later receipt cannot erase an earlier pause.
    slot_receipt_seen = False
    admitted_slots: set[str] = set()
    errored_slots: set[str] = set()
    for record in records:
        data = record.get("payload", {})
        if not isinstance(data, dict):
            return {}
        kind = data.get("type")
        if record.get("type") == "event_msg" and kind == "task_started":
            active = data.get("turn_id") == turn
            runs.clear()
            owner = ""
            pending_questions = 0
            question_receipts.clear()
            async_call = async_state = ""
            checkout = None
            checkout_binding = None
            slot_receipt_seen = False
            admitted_slots.clear()
            errored_slots.clear()
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
                if delivery_only and name == "request_user_input_async" and kind == "function_call":
                    if async_call or not isinstance(data.get("call_id"), str) or not data["call_id"]:
                        return {}
                    async_call = data["call_id"]
                    async_state = "called"
                elif name == NATIVE_QUESTION:
                    if async_call:
                        return {}
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
                        if async_call:
                            return {}
                        pending_questions += 1
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                output = data.get("output", "")
                if async_call and data.get("call_id") == async_call:
                    if async_state != "recorded" or not isinstance(output, str):
                        return {}
                    try:
                        receipt = json.loads(output)
                    except ValueError:
                        return {}
                    if not isinstance(receipt, dict) or set(receipt) != {"accepted"} or receipt["accepted"] is not True:
                        return {}
                    async_state = "accepted"
                # Host rejections can have no CommandExecution receipt. Do not retry them.
                if re.search(
                    r"(?:(?:denied|rejected) by (?:the )?user|user denied|approval denied|auto.?review.{0,40}reject)",
                    str(output),
                    re.I,
                ):
                    return {}
            elif kind == "message" and data.get("role") == "user" and (runs or slot_receipt_seen):
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
            if runs or slot_receipt_seen:
                return {}
            continue
        if item_type in {"Reasoning", "AgentMessage"}:
            if delivery_only and item.get("questions"):
                if (
                    async_state != "called"
                    or item.get("id") != async_call
                    or item.get("delivery") != "async"
                    or item.get("phase") != "final_answer"
                ):
                    return {}
                async_state = "recorded"
            continue
        # A host timer after async delivery neither resolves nor cancels the decision.
        if (
            item_type == "Extension"
            and async_state == "accepted"
            and item.get("kind") == "clock.sleep"
            and set(item) == {"type", "kind", "id", "durationMs"}
            and type(item["durationMs"]) is int
        ):
            continue
        if item_type == "FileChange":
            # Artifact writes (for example the review blueprint) emit this receipt.
            # A completed edit neither finishes the workflow nor requests a pause.
            if item.get("status") != "completed":
                return {}
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
        exit_code = item["exit_code"]
        args = _argv(item, plugin_root) if exit_code in (0, 1, 2) else []
        slot_helper = bool(args) and _coordinate(args[1], plugin_root) == _coordinate(
            "shared/review_checkout_slot.py", plugin_root
        )
        slot_receipt = _checkout_slot_receipt(args[2:], output, exit_code, cwd) if slot_helper else None
        # A validated helper receipt is routed by its fields: owner IDs and slot paths may contain these words.
        if exit_code != 0 and slot_receipt is None:
            if re.search(r"denied|not permitted|approval|retry.{0,30}exhaust", output, re.I):
                return {}
        if exit_code != 0 and not slot_helper:
            args = []
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
        if slot_helper:
            receipt = slot_receipt
            if receipt is None:
                continue
            slot_receipt_seen = True
            # A broken slot stays broken for the turn, in either order: a peer observation before or after
            # the error cannot overwrite it.
            if receipt.status == "error" and receipt.slot is not None:
                if receipt.slot in admitted_slots:
                    return {}
                errored_slots.add(receipt.slot)
            # Claim ownership can end when the workflow yields. Keep the original
            # checkout identity through release so re-entry cannot inherit a new slot.
            if checkout_binding is not None:
                same_slot = receipt.slot == checkout_binding.slot
                same_requester = receipt.requester == checkout_binding.requester
                if receipt.via_checkout and (same_requester or receipt.requester is None) and not same_slot:
                    return {}
                if receipt.action == "acquire" and not (same_requester and same_slot):
                    continue
            elif (
                checkout is not None
                and receipt.via_checkout
                and receipt.slot == checkout.slot
                and receipt.requester in (None, checkout.requester)
            ):
                checkout_binding = checkout
            checkout = _checkout_admission(checkout, receipt)
            if checkout_binding is None and checkout is not None and checkout.via_checkout:
                checkout_binding = checkout
            if checkout is not None:
                admitted_slots.add(checkout.slot)
                if checkout.state == "error" or checkout.slot in errored_slots:
                    return {}
            continue
        helper = _coordinate(args[1], plugin_root)
        if helper == _coordinate("shared/create_run.py", plugin_root):
            creation: dict[str, str] = {}
            arguments = iter(args[2:])
            valid_creation = False
            for argument in arguments:
                option, separator, value = argument.partition("=")
                if (
                    option
                    not in {
                        "--skill",
                        "--pr-number",
                        "--pr-url",
                        "--branch",
                        "--local-scope",
                        "--local-target",
                        "--promote-pr-run",
                    }
                    or option in creation
                ):
                    break
                if not separator:
                    value = next(arguments, "")
                if not value.strip() or value.startswith("-"):
                    break
                creation[option] = value
            else:
                skill = creation.get("--skill", "")
                identity_options = set(creation) - {"--skill"}
                if "--local-scope" in creation or "--local-target" in creation:
                    local_scope = creation.get("--local-scope")
                    allowed = {"--branch", "--local-scope"}
                    if local_scope in {"path", "commit"}:
                        allowed.add("--local-target")
                    if (
                        skill != "code-review"
                        or local_scope not in {"working-tree", "path", "commit"}
                        or identity_options != allowed
                    ):
                        continue
                elif "--pr-url" in creation:
                    if skill != "code-review" or identity_options != {"--pr-number", "--pr-url"}:
                        continue
                elif skill not in {"code-review", "code-remediate"} or len(identity_options) > 1:
                    continue
                number = creation.get("--pr-number")
                if number is not None and (not number.isascii() or not number.isdecimal() or int(number) <= 0):
                    continue
                # Promotion and creation are separate calls; duplicate or mixed-mode options cannot bind a run.
                valid_creation = True
            if not valid_creation:
                continue
            report = _host_output(output)
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
            owner = owner or skill
        elif helper == _coordinate("shared/find-review-report.py", plugin_root):
            # A lookup alone leaves the choice open. The producer confirms the exact
            # offered directory after a resume decision, including in a later turn.
            if args[2:3] == ["--latest-run"] and args[-2:-1] == ["--expect-run"]:
                selection = args[3:-2]
                pr = len(selection) == 2 and selection[0] == "--target"
                local = selection == ["--local-scope", "working-tree"] or (
                    len(selection) == 4
                    and selection[0] == "--local-scope"
                    and selection[1] in {"commit", "path"}
                    and selection[2] == "--local-target"
                )
                report = _host_output(output)
                if (pr or local) and report and "\n" not in report and "\r" not in report:
                    run = _coordinate(args[-1], cwd)
                    roots = tuple(
                        _coordinate(f".reports/codex/{name}", cwd) + "/" for name in ("code-review", "review")
                    )
                    if run.startswith(roots) and _coordinate(report, cwd) == run:
                        runs[run] = "code-review"
                        owner = owner or "code-review"
                continue
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
    # A later helper invocation cannot transfer ownership of the original workflow.
    if not active or pending_questions:
        return {}
    checkout_state = checkout.state if checkout is not None else ""
    if checkout_state == "stop" or (checkout_state != "peer" and CONTROLLING_STOP.search(stop_prose)):
        return {}
    if delivery_only:
        if owner not in runs.values() or (question_receipts and not async_call):
            return {}
        if async_call and async_state != "accepted":
            return {}
        return {
            "decision": "block",
            "reason": (
                "Recover question delivery only. Keep the dependent action paused. Check current eligibility against "
                "the workflow's recorded safeguards first. Recover required evidence where possible and correct any "
                "incomplete command or scope before asking. If eligible, preserve a still-visible, accurate question "
                "for a typed reply. Otherwise, when an eligible question needs delivery, present it once through a "
                "suitable control or end with the question and complete actionable choices in plain text. Keep work "
                "pending until an explicit answer. Runtime permission remains with its dedicated host control. "
                "Tool acceptance grants no permission."
            ),
        }
    # Before a run is bound there is no workflow to resume: a question that ends the turn stays the user's to answer.
    if owner not in runs.values() and TERMINAL_QUESTION.search(terminal_block):
        return {}
    if checkout_state == "peer":
        return {
            "decision": "block",
            "reason": (
                "The last checkout admission receipt reported a peer claim. This one-time diagnostic grants no "
                f"authority. {CONTROLLING_PAUSE} Honor any newer slot status. If acquisition was interrupted and its "
                "outcome is unknown, check `review_checkout_slot.py status --slot <retained locator>` first. While "
                "still queued, keep one cancellable `--queue` acquisition active under the original owner ID. "
                "If the host needs bounded waits, use `acquire --slot <retained locator> --owner <original owner ID> "
                "--wait-seconds 60`. Preserve the peer's claim. After acquisition, refresh PR state when applicable "
                "and resume the workflow."
            ),
        }
    if checkout_state == "acquired" and not owner:
        return {
            "decision": "block",
            "reason": (
                "The checkout slot was acquired before an active run was recorded. This one-time diagnostic grants no "
                f"authority. {CONTROLLING_PAUSE} Preserve the retained locator and original owner ID. Refresh PR state "
                "when applicable and resume the existing authorized workflow from its first unmet checkpoint."
            ),
        }
    if owner not in runs.values():
        return {}
    if owner == "code-remediate":
        next_step = (
            "Continue remediation selection, selected fixes, verification and delivery under the user's existing scope. "
            "Only if the parent already has explicit +review authorization, finish its needed nested review and admit "
            "the exact validated result before returning to remediation. A missing selection needs its normal question."
        )
    else:
        next_step = (
            "Continue the read-only review. Prepare eligible dependencies and continue independent inspection despite "
            "failed tests. Use disposable source copies for autofixing hooks. Finish reviewer reconciliation and the "
            "configured gates, promote the result, and deliver the validated tables from --complete-run. Disclose any "
            "missed blind checkpoint and attempt normal finalization."
        )
    return {
        "decision": "block",
        "reason": (
            "Codex Rig detected an unfinished workflow. This is a one-time diagnostic, not new authorization. "
            f"{CONTROLLING_PAUSE} Otherwise execute the first unmet checkpoint now under existing authority. "
            f"{next_step}"
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
