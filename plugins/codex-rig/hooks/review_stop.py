#!/usr/bin/env python3
"""Request one diagnostic continuation when review or remediation stops before closure.

## Purpose

Catch an observed failure mode where the parent starts review or remediation, leaves authorized stages unfinished, and
emits a final answer before the workflow's completion check. Instructions and artifact validators cannot intercept an
answer that bypasses those validators; the host Stop event supplies that interception point.

## Scope

Read the bounded host transcript supplied with the Stop event. For nested invocation paths, inspect only the presence
of enclosing Git markers to identify the session's checkout. Bind successful direct packaged helper executions to
the current session and turn, including run promotion and same-run completion. Creation may precede a bounded literal
read-only suffix batch; completion requires its own successful direct command. This is an advisory recovery prompt, not
an authorization or security boundary. Unknown lifecycle shapes, visible controlling stops and already-continued turns
produce no continuation. Known native answers for continuation, review entry and remediation require an outstanding
question and a distinct host receipt with matching decision and scope. An integrity restart can reactivate an observed
inspection hold for its original exclusive slot and owner. Checkout-slot receipts can bind recovery before run
creation and supersede an earlier observation at the same slot. Recovery ends for the turn on a terminal plain-text
question before run creation, a user message after an observed report lookup or validated slot receipt, or a helper
error at an admitted slot. Pause words inside receipt owner IDs or slot paths are data. Every command receipt accepts
the same host output endings. Standalone PR review and remediation bind the invoking checkout's
report storage to the requested PR. Remediation can resolve storage before admission and bind it at allocation.
An undelivered scope question can recover inventory preparation and question delivery only. The documented incomplete
handoff, or a scope question or menu anywhere in the message, keeps that status controlling.
On an in-place resumed PR, a checked original branch receipt plus the same retained run's selection render can restore
the active workflow. A submitted scope answer then needs the matching rendered inventory digest.
The same owner may release its PR claim before taking repository-wide exclusivity.
A confirmed historical PR report lookup may bind its original checkout. A terminal fork checkout receipt permits
reporting after the owner releases the matching claim. A collector-classified branch collision calls
for showing the local and remote differences.
Custom scope and free-text index answers stay outside recovery because the hook cannot verify the frozen inventory.
Occurrence accounting does not prove exact outer JavaScript call correlation. Transcript compatibility is deliberately
narrow because the host format is unstable.

Pause recognition also searches the assistant's own final prose, so it is model-controllable: ordinary or injected words
such as ``awaiting`` or ``denied`` can suppress continuation without host evidence of a stop. Ordinary checkout wait
wording yields to current peer evidence. Literal code-mode queue and polling calls can supply peer progress before
the terminal command receipt. A caller-interrupted queue can recover only under a matching process identity.
The advisory cannot enforce completion against model-controlled text.

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

import hashlib
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
    r"\b(?:awaiting|waiting for) (?:(?:another|the other|a peer) review to release (?:the )?)?"
    r"(?:the )?(?:(?:shared )?checkout (?:slot|admission)|other checkout owner)\b",
    re.IGNORECASE,
)
DECISION_WAIT = re.compile(
    r"\b(?:(?:awaits?|awaiting|waiting for) (?:your|the) (?:decision|answer)|"
    r"waiting for your approval before running the command|pending user decision)\b",
    re.IGNORECASE,
)
SELECTION_STATUS = re.compile(r"\bawaiting selection(?=[.,;:*\n]|$)", re.IGNORECASE)
# The documented blocked or user-stopped remediation handoff also reports findings awaiting selection.
INCOMPLETE_HANDOFF = re.compile(
    r"\bremediation remains incomplete\b|\bfirst unmet checkpoint\b|\bat your request\b", re.IGNORECASE
)
REPLY_INSTRUCTION = re.compile(r"(?:^|[?!.]\s+)(?:please\s+)?(?:reply|choose|select|enter)\s+\S", re.IGNORECASE | re.M)
ACTION_LABEL = r"approve(?: always)?|deny|hold|continue|skip|stop|resume last(?: one)?|start fresh|new review"
CHOICE_LINE = re.compile(
    rf"^\s*(?:(?:[-*]|\d+[.)])\s+|(?=[`* ]*(?:{ACTION_LABEL})[`* ]*=))[`*]*({ACTION_LABEL})\b", re.IGNORECASE | re.M
)
INLINE_CHOICES = re.compile(rf"^[`* ]*(?:{ACTION_LABEL})[`* ]*(?:/|\bor\b)[`* ]*(?:{ACTION_LABEL})\b", re.IGNORECASE)
SCOPE_QUESTION = "Which findings should I remediate?"
COMMIT_QUESTION = "How should verified remediation-owned changes be grouped into commits?"
REMEDIATION_QUESTIONS = (SCOPE_QUESTION, COMMIT_QUESTION, "Which finding indexes should I remediate?")
SCOPE_CHOICES = ["All", "Required", "Suggestions", "Custom selection"]
COMMIT_MODES = (
    "all at once",
    "group findings by topic",
    "each finding as a separate commit",
    "decide after verification",
    "leave unstaged",
)
REMEDIATION_LABEL = "|".join(re.escape(label) for label in (*SCOPE_CHOICES, *COMMIT_MODES))
REMEDIATION_MENU_OPTION = re.compile(
    rf"(?:^\s*(?:(?:[-*]|\d+[.)])\s+)?|/\s*)[`*]*({REMEDIATION_LABEL})[`*]*(?=\s*(?:$|[/=:—–-]))",
    re.IGNORECASE | re.M,
)
QUESTION = re.compile(r"request_user_input|ask_user|interrupt_agent")
# A bare ask_user is also used in ordinary prose. Qualified names, invocation and alias syntax remain visible.
QUESTION_REFERENCE = re.compile(r"request_user_input|interrupt_agent|__ask_user|\bask_user\b(?=\s*(?:[^\w\s]|$))")
NESTED_QUESTION = re.compile(
    r"\btools\s*\.\s*(\w*(?:request_user_input(?:_async)?|ask_user|interrupt_agent))\b(\s*\()?"
)
NATIVE_QUESTION = "mcp__codex_rig_input__ask_user"
QUESTION_DISCOVERY = re.compile(
    r"(?:^|(?<=;))\s*text\(ALL_TOOLS\.filter\(([A-Za-z_]\w*)\s*=>\s*"
    r'(?:/[A-Za-z_|]+/\.test\(\1\.name\)|\1\.name\.includes\("ask_user"\))\)\)'
    r"(?=\s*(?:;|$))"
)
# A literal shell command is data in code mode. Exclude only this complete, non-interpolating call and projection.
LITERAL_SHELL_CALL = re.compile(
    r'\s*const (\w+)\s*=\s*await tools\.exec_command\(\{cmd:"(?:[^"\\]|\\.)*"\}\);\s*text\(\1\.output\);?\s*'
)
# Only literal tool calls with the known output projection can bind a still-running code-mode process.
QUEUE_START = re.compile(
    r'\s*const (\w+)\s*=\s*await tools\.exec_command\(\{\s*cmd:\s*("(?:[^"\\]|\\.)*"),\s*'
    r'(?:workdir:\s*("(?:[^"\\]|\\.)*"),\s*)?yield_time_ms:\s*\d+(?:,\s*workdir:\s*("(?:[^"\\]|\\.)*"))?\s*\}\);'
    r"\s*text\(\1\.output\);\s*text\(\1\.session_id\);?\s*"
)
QUEUE_STDIN = re.compile(
    r'\s*const (\w+)\s*=\s*await tools\.write_stdin\(\{session_id:(\d+),\s*chars:"(|\\u0003)",\s*yield_time_ms:\d+\}\);'
    r"\s*(?:text\(\1\.output\);\s*)?text\(\1\.(session_id|exit_code)\);?\s*"
)
# A confirmed finder receipt can bind a historical private report. New reviews use the invoking checkout.
HISTORICAL_ROOT = re.compile(r"/codex-rig/review-workspaces/[a-f0-9]{64}$")
GITHUB_REPOSITORY = r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
PR_URL = re.compile(rf"(?i:https://github\.com)/{GITHUB_REPOSITORY}/pull/[1-9][0-9]*")
PR_QUESTION_SUBJECT = re.compile(
    rf"(?:(?:Review|Remediate) )?PR {GITHUB_REPOSITORY}#[1-9][0-9]*(?: — [^\s][^\r\n]*)?\r?\n\r?\n"
)


WRITER_SLOT = re.compile(r"(?P<common>.+)/codex-review-writers/[a-f0-9]{64}/codex-review-slot$")
TARGET_SLOT = re.compile(r"(?P<common>.+)/codex-review-targets/(?P<digest>[a-f0-9]{64})/codex-review-slot$")


def _target_digest(url: str) -> str:
    """Match the ordinary slot helper's canonical PR identity."""
    return hashlib.sha256(url.lower().encode("ascii")).hexdigest()


def _slot_common(slot: str) -> str:
    """Identify the repository common directory from a validated target, writer or exclusive locator."""
    scoped = TARGET_SLOT.fullmatch(slot) or WRITER_SLOT.fullmatch(slot)
    return scoped["common"] if scoped else slot.rsplit("/", 1)[0]


def _report_root_matches(root: str, checkout: str, slot: str) -> bool:
    """Bind the report-root helper's storage receipt to the admitted checkout or its Git directory."""
    common = _slot_common(slot)
    return (
        root == checkout + "/.reports/codex"
        or root == common + "/codex-rig/reports"
        or re.fullmatch(re.escape(common) + r"/worktrees/[^/]+/codex-rig/reports", root) is not None
    )


def _checked_remediation_run(arguments: list[str], output: str, cwd: str, slot: str, target: str) -> str:
    """Recognize an in-place retained PR run checked against its original branch and exact current revision."""
    if len(arguments) != 5 or arguments[:2] != ["check", "--receipt"] or arguments[3] != "--expected-head":
        return ""
    receipt = json.loads(_host_output(output))
    number = target.rsplit("/", 1)[-1]
    path = _coordinate(arguments[2], cwd)
    # Only this retained storage layout has independent evidence on resume.
    root = _slot_common(slot) + "/codex-rig/reports/code-remediate/"
    if (
        not re.fullmatch(re.escape(root) + rf"pr-{number}/run-[0-9]{{3,}}/remediation-branch\.json", path)
        or not isinstance(receipt, dict)
        or type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != 2
        or receipt.get("status") != "prepared"
        or type(receipt.get("pr_number")) is not int
        or str(receipt["pr_number"]) != number
        or not isinstance(receipt.get("pr_url"), str)
        or receipt["pr_url"].lower() != target
        or not isinstance(receipt.get("worktree"), str)
        or _coordinate(receipt["worktree"], cwd) != cwd
        or not (PureWindowsPath if PureWindowsPath(cwd).drive else PurePosixPath)(receipt["worktree"]).is_absolute()
        or not re.fullmatch(r"[0-9a-f]{40}", arguments[4])
        or receipt.get("initial_head") != arguments[4]
    ):
        return ""
    return path.rsplit("/", 1)[0]


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
    scope: str | None


def _question_body(text: str) -> str | None:
    """Separate one PR display subject from decision text, preserving older questions verbatim."""
    prefix = PR_QUESTION_SUBJECT.match(text)
    if prefix is None:
        return text
    body = text[prefix.end() :]
    return body if body.strip() and not PR_QUESTION_SUBJECT.match(body) else None


def _resolved_question(item: dict, remediation_active: bool) -> bool:
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
    if not isinstance(answer, str) or answer not in options:
        return False
    known = (
        answer == "Continue"
        or options == ["Resume last", "Start fresh"]
        or (options == ["New review", "Resume last one", "Stop"] and answer in {"New review", "Resume last one"})
    )
    question = arguments.get("question")
    if isinstance(question, str):
        question = _question_body(question)
        if question is None:
            return False
    if remediation_active and isinstance(question, str):
        if question.startswith(SCOPE_QUESTION):
            known = options == SCOPE_CHOICES and answer != "Custom selection"
        elif question.startswith(COMMIT_QUESTION):
            # The recommended choice leads the menu and can change with the project's standing commit grant.
            known = len(options) == len(COMMIT_MODES) and all(mode in options for mode in COMMIT_MODES)
    if not known:
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


def _enclosing_checkout(cwd: str) -> str:
    """Recognize the nearest native checkout without running Git or reading repository-controlled file contents."""
    directory = Path(cwd)
    if directory.is_absolute():
        for parent in (directory, *directory.parents):
            try:
                marker = (parent / ".git").lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(marker.st_mode) or stat.S_ISREG(marker.st_mode):
                return _coordinate(str(parent), cwd)
            break
    return _coordinate(cwd, cwd)


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
            r"(?<!\S)(--(?:complete-run|codex-home|parent-thread-id|pr-number|pr-url|root|branch|skill|local-scope|local-target|promote-pr-run|out|target))=([\"'])(.*?)\2(?=\s|$)",
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


def _checkout_slot_receipt(
    args: list[str], output: str, exit_code: int | None, cwd: str, session_subdir: str | None = None
) -> CheckoutSlotReceipt | None:
    """Validate the packaged helper's invocation, coordinates, exit status and receipt fields together."""
    scope = None
    if len(args) > 4 and args[0] in {"acquire", "status"} and args[1] == "--checkout" and args[3] == "--target":
        if not PR_URL.fullmatch(args[4]):
            return None
        scope = args[4].lower()
        args = args[:3] + args[5:]
    elif (
        len(args) >= 4
        and args[0] in {"acquire", "status"}
        and args[1] == "--checkout"
        and args[3] in {"--local", "--writer"}
    ):
        scope = args[3].removeprefix("--")
        args = args[:3] + args[4:]
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
        if via_checkout and requested not in {_coordinate(cwd, cwd), session_subdir}:
            return None
        fields = {"status", "slot", "owner"}
        if exit_code is None and isinstance(receipt, dict) and receipt.get("queue") == "waiting":
            fields.add("queue")
        if exit_code == 2:
            fields.add("error")
        if not isinstance(receipt, dict) or set(receipt) != fields:
            return None
        slot, status, holder = receipt["slot"], receipt["status"], receipt["owner"]
        error = exit_code == 2 and status == "error" and holder is None and isinstance(receipt["error"], str)
        if slot is None and error and via_checkout:
            return CheckoutSlotReceipt(action, requester, None, status, holder, via_checkout, scope)
        if (
            not isinstance(slot, str)
            or not path_type(slot).is_absolute()
            or path_type(slot).name != "codex-review-slot"
        ):
            return None
        slot = _coordinate(slot, cwd)
        if path_type(slot).parent.parent.name == "codex-review-writers":
            if not WRITER_SLOT.fullmatch(slot) or (via_checkout and scope != "writer"):
                return None
            scope = "writer"
        else:
            target_match = TARGET_SLOT.fullmatch(slot)
            if scope == "writer" or (
                via_checkout
                and (
                    (scope is not None and not target_match)
                    or (scope is None and target_match)
                    or (scope not in {None, "local"} and target_match["digest"] != _target_digest(scope))
                )
            ):
                return None
        if not via_checkout and requested != slot:
            return None
    except ValueError:
        return None

    if holder is not None and (not isinstance(holder, str) or not SLOT_OWNER.fullmatch(holder)):
        return None
    if error:
        return CheckoutSlotReceipt(action, requester, slot, status, holder, via_checkout, scope)
    observation = status == "busy" or (status == "inspection-required" and holder is not None)
    if (
        (exit_code is None and wait == ["--queue"] and observation and holder not in (None, requester))
        or (action == "acquire" and exit_code == 0 and status == "acquired" and holder == requester)
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
        return CheckoutSlotReceipt(action, requester, slot, status, holder, via_checkout, scope)
    return None


def _checkout_admission(previous: CheckoutAdmission | None, receipt: CheckoutSlotReceipt) -> CheckoutAdmission | None:
    """Update admission only from the same locator and an applicable requester or holder."""
    action, requester, slot, status, holder, via_checkout, _ = receipt
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
    if previous.state in {"peer", "available"} and (
        (action == "status" and status == "free")
        or (action == "release" and status == "released" and requester == previous.holder)
    ):
        return previous._replace(state="available", holder=None, via_checkout=via_checkout)
    if status in {"busy", "inspection-required"}:
        if previous.state == "acquired" and holder == previous.requester:
            return previous._replace(via_checkout=via_checkout)
        if previous.state in {"peer", "available"} and holder not in (None, previous.requester):
            return previous._replace(state="peer", holder=holder, via_checkout=via_checkout)
    return None


def _remediation_collection_target(args: list[str], run: str, number: str, cwd: str) -> str:
    """Bind a packaged remediation checkout command to this invocation's selected PR and run."""
    if args.count("--checkout") != 1:
        return ""
    options: dict[str, str] = {}
    arguments = iter(args)
    for argument in arguments:
        if argument == "--checkout":
            continue
        key, sep, value = argument.partition("=")
        if key not in {"--target", "--out", "--checkout-mode", "--timeout-seconds"} or key in options:
            return ""
        options[key] = value if sep else next(arguments, "")
    url = options.get("--target", "")
    destination = options.get("--out", "")
    if (
        not PR_URL.fullmatch(url)
        or url.rsplit("/", 1)[-1] != number
        or options.get("--checkout-mode") != "remediate"
        or not destination
    ):
        return ""
    try:
        return url.lower() if _coordinate(destination, cwd) in {run, run + "/pr"} else ""
    except ValueError:
        return ""


def _fork_checkout_status(output: str, target: str) -> str:
    """Identify a bound fork-checkout stop, including a collision with retained branch-comparison evidence."""
    try:
        receipt = json.loads(_host_output(output))
    except ValueError:
        return ""
    if not isinstance(receipt, dict) or set(receipt) != {"status", "pr_url"}:
        return ""
    reported, status = receipt["pr_url"], receipt["status"]
    if (
        isinstance(reported, str)
        and PR_URL.fullmatch(reported)
        and reported.lower() == target
        and status in ("remediation-fork-checkout-stopped", "remediation-fork-checkout-branch-collision")
    ):
        return status
    return ""


def evaluate(payload: dict, records: list[dict], plugin_root: str) -> dict:
    """Continue positively bound review or remediation work without overriding a controlling pause."""
    if payload.get("hook_event_name") != "Stop" or payload.get("stop_hook_active") is not False:
        return {}
    session, turn, cwd = (payload.get(name) for name in ("session_id", "turn_id", "cwd"))
    message = payload.get("last_assistant_message")
    if not all(isinstance(value, str) and value for value in (session, turn, cwd, message, plugin_root)):
        return {}
    message = _question_body(message)
    if message is None:
        return {}
    delivery_only = DECISION_WAIT.search(message) is not None
    stop_prose = DECISION_WAIT.sub("", message)
    selection_status = 0
    # A stop handoff keeps its status wording, so the wait stays controlling as before selection recovery existed.
    if not INCOMPLETE_HANDOFF.search(message):
        stop_prose, selection_status = SELECTION_STATUS.subn("", stop_prose)
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
    invoking_cwd = _coordinate(cwd, cwd)
    checkout_root = _enclosing_checkout(cwd)
    review_cwd = checkout_root
    historical_pr = ""
    pr_reports: tuple[str, str] | None = None
    early_pr_reports: tuple[str, str] | None = None
    report_lookup_seen = False
    unconfirmed_remediation = ""
    checked_run = checked_scope_digest = ""
    active = False
    runs: dict[str, str] = {}
    superseded_review = ""
    owner = ""
    remediation_pr: tuple[str, str] | None = None
    remediation_source = checkout_root
    terminal_checkout = ""
    ordinary_slot = released_slot = review_scope = ""
    pending_questions = 0
    scope_answered = False
    question_receipts: set[str] = set()
    async_call = ""
    async_state = ""
    checkout: CheckoutAdmission | None = None
    checkout_binding: CheckoutAdmission | None = None
    # Turn-scoped slot evidence survives an admission reset, so a later receipt cannot erase an earlier pause.
    slot_receipt_seen = False
    writer_claims: set[tuple[str, str]] = set()
    admitted_slots: set[str] = set()
    errored_slots: set[str] = set()
    # Call IDs bind code-mode results. Process IDs join polls and caller interrupts to their own command.
    queue_calls: dict[str, tuple[list[str], str]] = {}
    queue_processes: dict[str, list[str]] = {}
    caller_interrupts: dict[str, str] = {}

    def checkout_for(directory: str) -> str:
        """Bind the session directory to its nearest checkout while keeping relative artifact paths command-local."""
        return checkout_root if directory == invoking_cwd else directory

    def slot_result(arguments: list[str], output: str, status: int | None) -> CheckoutSlotReceipt | None:
        """Read a slot result for the active repository, including the session's original subdirectory spelling."""
        alias = invoking_cwd if review_cwd == checkout_root else None
        return _checkout_slot_receipt(arguments, output, status, review_cwd, alias)

    def observe_slot(receipt: CheckoutSlotReceipt) -> bool:
        """Apply validated progress and terminal receipts to the same turn-scoped admission."""
        nonlocal \
            checkout, \
            checkout_binding, \
            slot_receipt_seen, \
            terminal_checkout, \
            ordinary_slot, \
            released_slot, \
            review_scope
        if (
            historical_pr
            and receipt.slot is not None
            and receipt.slot != _coordinate(".git/codex-review-slot", review_cwd)
        ):
            # A private workspace owns its own physical .git. Other slots cannot become this invocation's evidence.
            return not receipt.via_checkout
        slot_receipt_seen = True
        if receipt.scope == "writer":
            # An in-place writer is independent of PR admission. Only a successful release of the acquired
            # locator by its original owner discharges the writer obligation. Respect inspection/error stops.
            if receipt.slot is not None and receipt.requester is not None:
                claim = (receipt.requester, receipt.slot)
                if receipt.action == "acquire" and receipt.status == "acquired":
                    writer_claims.add(claim)
                elif receipt.action == "release" and receipt.status == "released":
                    writer_claims.discard(claim)
                elif claim in writer_claims and receipt.status in {"inspection-required", "error", "not-owner"}:
                    return False
            return not (
                receipt.action == "status"
                and receipt.status in {"inspection-required", "error"}
                and any(slot == receipt.slot for _, slot in writer_claims)
            )
        # A broken slot stays broken for the turn, in either order: a peer observation before or after
        # the error cannot overwrite it.
        if receipt.status == "error" and receipt.slot is not None:
            if receipt.slot in admitted_slots:
                return False
            errored_slots.add(receipt.slot)
        # Claim ownership can end when the workflow yields. Keep the original
        # checkout identity through release so re-entry cannot inherit a new slot.
        if checkout_binding is not None:
            # Only a proved release permits a transition. Both locators must identify the same common directory,
            # owner and already-established PR, even when the second command uses a retained locator.
            if (
                checkout is None
                and receipt.action == "acquire"
                and receipt.requester == checkout_binding.requester
                and released_slot == checkout_binding.slot
                and ordinary_slot
                and receipt.slot is not None
                and {released_slot, receipt.slot} == {ordinary_slot, _slot_common(ordinary_slot) + "/codex-review-slot"}
                and (receipt.via_checkout or receipt.slot in admitted_slots)
            ):
                checkout_binding = None
                receipt = receipt._replace(via_checkout=True)
        if checkout_binding is not None:
            same_slot = receipt.slot == checkout_binding.slot
            same_requester = receipt.requester == checkout_binding.requester
            if receipt.via_checkout and (same_requester or receipt.requester is None) and not same_slot:
                return False
            if receipt.action == "acquire" and not (same_requester and same_slot):
                return True
        elif (
            checkout is not None
            and receipt.via_checkout
            and receipt.slot == checkout.slot
            and receipt.requester in (None, checkout.requester)
        ):
            checkout_binding = checkout
        if receipt.action == "acquire":
            terminal_checkout = ""
            released_slot = ""
            if receipt.via_checkout and receipt.slot and TARGET_SLOT.fullmatch(receipt.slot):
                if ordinary_slot and ordinary_slot != receipt.slot:
                    return False
                ordinary_slot = receipt.slot
                if receipt.scope:
                    review_scope = receipt.scope
        elif (
            terminal_checkout in {"failed", "collision"}
            and checkout is not None
            and checkout.state == "acquired"
            and receipt.action == "release"
            and receipt.status == "released"
            and (receipt.slot, receipt.requester) == (checkout.slot, checkout.requester)
        ):
            terminal_checkout = "released-collision" if terminal_checkout == "collision" else "released"
        if (
            checkout is not None
            and receipt.action == "release"
            and receipt.status == "released"
            and (receipt.requester, receipt.slot) == (checkout.requester, checkout.slot)
        ):
            released_slot = checkout.slot
        checkout = _checkout_admission(checkout, receipt)
        if checkout_binding is None and checkout is not None and checkout.via_checkout:
            checkout_binding = checkout
        if checkout is not None:
            admitted_slots.add(checkout.slot)
            if checkout.state == "error" or checkout.slot in errored_slots:
                return False
        return True

    for record in records:
        data = record.get("payload", {})
        if not isinstance(data, dict):
            return {}
        kind = data.get("type")
        if record.get("type") == "event_msg" and kind == "task_started":
            active = data.get("turn_id") == turn
            runs.clear()
            superseded_review = ""
            review_cwd = checkout_root
            historical_pr = ""
            pr_reports = None
            early_pr_reports = None
            report_lookup_seen = False
            unconfirmed_remediation = ""
            checked_run = checked_scope_digest = ""
            remediation_pr = None
            remediation_source = checkout_root
            terminal_checkout = ""
            ordinary_slot = released_slot = review_scope = ""
            owner = ""
            pending_questions = 0
            scope_answered = False
            question_receipts.clear()
            queue_calls.clear()
            queue_processes.clear()
            caller_interrupts.clear()
            async_call = async_state = ""
            checkout = None
            checkout_binding = None
            slot_receipt_seen = False
            writer_claims.clear()
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
                    call_id = data.get("call_id")
                    if name == "exec" and kind == "custom_tool_call" and isinstance(call_id, str) and call_id:
                        if match := QUEUE_START.fullmatch(source):
                            shell = ["pwsh", "-Command"] if PureWindowsPath(cwd).drive else ["sh", "-c"]
                            args = _argv({"command": [*shell, json.loads(match[2])]}, plugin_root)
                            workdir = match[3] or match[4]
                            if (
                                not (match[3] and match[4])
                                and checkout_for(_coordinate(json.loads(workdir) if workdir else cwd, cwd))
                                == review_cwd
                                and args
                                and args[-1] == "--queue"
                                and _coordinate(args[1], plugin_root)
                                == _coordinate("shared/review_checkout_slot.py", plugin_root)
                            ):
                                queue_calls[call_id] = args[2:], ""
                        elif match := QUEUE_STDIN.fullmatch(source):
                            process = match[2]
                            if match[3]:
                                caller_interrupts[call_id] = process
                            elif process in queue_processes and match[4] == "session_id":
                                queue_calls[call_id] = queue_processes[process], process
                    # Remove just the literal discovery statement so adjacent invocations still count.
                    if name == "exec" and kind == "custom_tool_call":
                        source = QUESTION_DISCOVERY.sub("", source)
                        if LITERAL_SHELL_CALL.fullmatch(source):
                            source = ""
                    if QUESTION_REFERENCE.search(NESTED_QUESTION.sub("", source)):
                        return {}
                    for question in NESTED_QUESTION.finditer(source):
                        if question[1] != NATIVE_QUESTION or not question[2] or name != "exec":
                            return {}
                        if async_call:
                            return {}
                        pending_questions += 1
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                output = data.get("output", "")
                call_id = data.get("call_id")
                if isinstance(call_id, str):
                    caller_interrupts.pop(call_id, None)
                    pending = queue_calls.pop(call_id, None)
                    if (
                        pending
                        and isinstance(output, list)
                        and len(output) == 3
                        and all(
                            isinstance(part, dict)
                            and part.get("type") == "input_text"
                            and isinstance(part.get("text"), str)
                            for part in output
                        )
                    ):
                        args, expected_process = pending
                        process = output[2]["text"]
                        if re.fullmatch(r"[0-9]+", process) and expected_process in ("", process):
                            queue_processes[process] = args
                            if receipt := slot_result(args, output[1]["text"], None):
                                if not observe_slot(receipt):
                                    return {}
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
            elif (
                kind == "message"
                and data.get("role") == "user"
                and (runs or historical_pr or early_pr_reports or slot_receipt_seen or queue_calls or queue_processes)
            ):
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
            if runs or historical_pr or early_pr_reports or slot_receipt_seen or queue_calls or queue_processes:
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
                or not _resolved_question(item, owner == "code-remediate" and owner in runs.values())
            ):
                return {}
            question_receipts.add(receipt_id)
            pending_questions -= 1
            if item["status"] == "completed" and item["result"]["structuredContent"]["answer"] in {
                "Start fresh",
                "New review",
            }:
                # Keep the prior run pending until a verified replacement allocation. A joined remediation
                # stays active, and ambiguous concurrent reviews retain their separate completion obligations.
                reviews = [run for run, skill in runs.items() if skill == "code-review"]
                superseded_review = reviews[0] if len(reviews) == 1 else ""
            if item["status"] == "completed" and item["arguments"]["options"] == SCOPE_CHOICES:
                if checked_run and item["arguments"]["scope_digest"] != checked_scope_digest:
                    return {}
                scope_answered = True
            continue
        if item_type != "CommandExecution":
            return {}
        if item.get("status") not in {"completed", "failed"} or type(item.get("exit_code")) is not int:
            return {}
        output = item.get("aggregated_output", "")
        if not isinstance(output, str):
            return {}
        process = str(item.get("process_id"))
        queue_processes.pop(process, None)
        exit_code = item["exit_code"]
        args = _argv(item, plugin_root)
        if args[2:3] == ["report-root"] and _coordinate(args[1], plugin_root) == _coordinate(
            "shared/review_code_safety.py", plugin_root
        ):
            report_lookup_seen = True
            if unconfirmed_remediation and exit_code != 0:
                return {}
        collector = bool(args) and _coordinate(args[1], plugin_root) == _coordinate("shared/collect_pr.py", plugin_root)
        if collector and not {"-h", "--help"}.intersection(args[2:]):
            # A later collector attempt supersedes the previous terminal receipt, even if it fails differently.
            terminal_checkout = ""
        slot_helper = bool(args) and _coordinate(args[1], plugin_root) == _coordinate(
            "shared/review_checkout_slot.py", plugin_root
        )
        slot_receipt = slot_result(args[2:], output, exit_code) if slot_helper else None
        if checked_run and args and exit_code != 0:
            failed_action = (_coordinate(args[1], plugin_root), args[2] if len(args) > 2 else "")
            if failed_action in {
                (_coordinate("shared/remediation_branch.py", plugin_root), "check"),
                (_coordinate("shared/final_handoff.py", plugin_root), "selection"),
            }:
                return {}
        if slot_helper and args[-1] == "--queue" and exit_code == 130:
            # Only an assistant-issued interrupt for this process can recover from its last peer observation.
            # A user or host cancellation, including an uncorrelated signal, remains controlling.
            if process not in caller_interrupts.values():
                return {}
            progress, traceback, tail = output.partition("\nTraceback (most recent call last):")
            if traceback and tail.rstrip().endswith("\nKeyboardInterrupt"):
                slot_receipt = slot_result(args[2:], progress, None)
            if slot_receipt is None:
                return {}
        # A validated helper receipt is routed by its fields: owner IDs and slot paths may contain these words.
        if exit_code != 0 and slot_receipt is None:
            if re.search(r"denied|not permitted|approval|retry.{0,30}exhaust", output, re.I):
                return {}
        if exit_code != 0 and not (slot_helper or (collector and exit_code == 2)):
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
        command_cwd = _coordinate(command_cwd, cwd)
        command_checkout = checkout_for(command_cwd)
        helper = _coordinate(args[1], plugin_root)
        finder = helper == _coordinate("shared/find-review-report.py", plugin_root)
        caller_lookup = finder and args[2:3] == ["--latest-run"] and command_checkout == checkout_root
        if (
            command_checkout != review_cwd
            and not caller_lookup
            and not (finder and historical_pr and command_checkout == checkout_root and "--complete-run" in args[2:])
            and not (collector and command_checkout == remediation_source)
            and not (
                slot_helper
                and command_checkout == checkout_root
                and "--slot" in args[2:]
                and checkout_binding is not None
            )
        ):
            continue
        admitted_pr = (
            checkout is not None
            and checkout.state == "acquired"
            and checkout.via_checkout
            and checkout.slot == ordinary_slot
            and PR_URL.fullmatch(review_scope)
            and command_cwd == review_cwd == checkout_root
        )
        if (
            admitted_pr
            and helper == _coordinate("shared/remediation_branch.py", plugin_root)
            and args[2:3] == ["check"]
            and (not owner or checked_run)
        ):
            if item["status"] != "completed":
                return {}
            checked = _checked_remediation_run(args[2:], output, command_cwd, ordinary_slot, review_scope)
            if checked_run and checked != checked_run:
                return {}
            checked_run = checked
            continue
        if (
            admitted_pr
            and checked_run
            and helper == _coordinate("shared/final_handoff.py", plugin_root)
            and args[2:4] == ["selection", "--input"]
            and len(args) == 7
            and args[5] == "--out-scope"
        ):
            receipt = json.loads(_host_output(output))
            if (
                item["status"] != "completed"
                or _coordinate(args[4], command_cwd) != checked_run + "/selection.json"
                or _coordinate(args[6], command_cwd) != checked_run + "/resolution-scope.md"
                or not isinstance(receipt, dict)
                or receipt.get("status") != "pass"
                or not all(
                    isinstance(receipt.get(k), str) and re.fullmatch(r"[0-9a-f]{64}", receipt[k])
                    for k in ("input_sha256", "rendered_sha256")
                )
                or (owner and (owner != "code-remediate" or runs.get(checked_run) != owner))
            ):
                return {}
            checked_scope_digest = receipt["input_sha256"]
            runs[checked_run] = owner = "code-remediate"
            continue
        if helper == _coordinate("shared/contributor_run_integrity.py", plugin_root):
            if (
                exit_code == 0
                and checkout is not None
                and checkout.state == "stop"
                and checkout.holder == checkout.requester
                and not (WRITER_SLOT.fullmatch(checkout.slot) or TARGET_SLOT.fullmatch(checkout.slot))
                and args[2:3] == ["restart-ref-fast-forward"]
                and len(args) == 19
                and set(args[3::2])
                == {"--snapshot", "--expect", "--branch", "--old-oid", "--new-oid", "--out", "--slot", "--owner"}
            ):
                options = dict(zip(args[3::2], args[4::2]))
                try:
                    proof = json.loads(_host_output(output))
                    if (
                        isinstance(proof, dict)
                        and proof.get("ok") is True
                        and proof.get("status") == "restarted"
                        and isinstance(proof.get("sha256"), str)
                        and re.fullmatch(r"[0-9a-f]{64}", proof["sha256"])
                        and proof.get("owner") == options["--owner"] == checkout.requester
                        and isinstance(proof.get("slot"), str)
                        and _coordinate(proof["slot"], command_cwd) == checkout.slot
                        and _coordinate(options["--slot"], command_cwd) == checkout.slot
                    ):
                        checkout = checkout._replace(state="acquired")
                except ValueError:
                    pass
            continue
        if (
            helper == _coordinate("shared/remediation_branch.py", plugin_root)
            and exit_code == 0
            and remediation_pr is not None
            and command_checkout == review_cwd
            and checkout is not None
            and checkout.state == "acquired"
            and args[2:4] == ["worktree", "--receipt"]
            and len(args) == 5
            and _coordinate(args[4], command_cwd)
            in {
                remediation_pr[0] + "/remediation-worktree.json",
                remediation_pr[0] + "/recovery-worktree.json",
            }
        ):
            # The allocator verifies both ends of the linked worktree registration before printing this receipt.
            try:
                receipt = json.loads(_host_output(output))
                path_type = PureWindowsPath if PureWindowsPath(cwd).drive else PurePosixPath
                if (
                    isinstance(receipt, dict)
                    and type(receipt.get("schema_version")) is int
                    and receipt["schema_version"] == 1
                    and all(
                        isinstance(receipt.get(key), str) for key in ("invoking_checkout", "git_common_dir", "worktree")
                    )
                    and _coordinate(receipt["invoking_checkout"], cwd) == review_cwd
                    and _coordinate(receipt["git_common_dir"], cwd) == _slot_common(checkout.slot)
                    and path_type(receipt["worktree"]).is_absolute()
                ):
                    remediation_source = _coordinate(receipt["worktree"], cwd)
                    terminal_checkout = ""
            except ValueError:
                pass
            continue
        if collector:
            if (
                remediation_pr is not None
                and owner == "code-remediate"
                and command_checkout == remediation_source
                and checkout is not None
                and checkout.state == "acquired"
                and checkout.via_checkout
                and not {"--help", "-h"}.intersection(args[2:])
            ):
                target = _remediation_collection_target(args[2:], *remediation_pr, command_cwd)
                if (
                    target
                    and (not historical_pr or target == historical_pr)
                    and (not pr_reports or target == pr_reports[1])
                    and exit_code == 2
                    and (status := _fork_checkout_status(output, target))
                ):
                    terminal_checkout = "failed"
                    if status == "remediation-fork-checkout-branch-collision":
                        terminal_checkout = "collision"
            continue
        if slot_helper:
            receipt = slot_receipt
            if receipt is None:
                continue
            if not observe_slot(receipt):
                return {}
            continue
        if helper == _coordinate("shared/review_code_safety.py", plugin_root):
            if (
                exit_code == 0
                and owner in {"", "code-review", "code-remediate"}
                and command_checkout == review_cwd
                and len(args) == 7
                and args[2:4] == ["report-root", "--checkout"]
                and args[5] == "--pr-url"
                and checkout_for(_coordinate(args[4], command_cwd)) == review_cwd
                and PR_URL.fullmatch(args[6])
                and (not historical_pr or historical_pr == args[6].lower())
                and (not review_scope or review_scope == args[6].lower())
            ):
                # This helper attests the storage location only. Source work and slot operations stay with the caller.
                report_base = _host_output(output)
                path_type = PureWindowsPath if PureWindowsPath(cwd).drive else PurePosixPath
                if path_type(report_base).is_absolute() and not re.search(r"[\r\n]", report_base):
                    coordinate = _coordinate(report_base, cwd)
                    binding = (coordinate, args[6].lower())
                    if unconfirmed_remediation:
                        if unconfirmed_remediation.rsplit("/", 2)[0] != coordinate or owner:
                            return {}
                        runs[unconfirmed_remediation] = owner = "code-remediate"
                        remediation_pr = (unconfirmed_remediation, args[6].rsplit("/", 1)[1])
                        pr_reports = binding
                        unconfirmed_remediation = ""
                    elif not slot_receipt_seen and not historical_pr:
                        if early_pr_reports is not None and early_pr_reports != binding:
                            return {}
                        early_pr_reports = binding
                    elif (
                        checkout
                        and checkout.state == "acquired"
                        and checkout.via_checkout
                        and _report_root_matches(coordinate, review_cwd, checkout.slot)
                    ):
                        if pr_reports is not None and pr_reports != binding:
                            return {}
                        pr_reports = binding
            if unconfirmed_remediation and args[2:3] == ["report-root"]:
                return {}
            continue
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
                        "--root",
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
                identity_options = set(creation) - {"--skill", "--root"}
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
            if review_scope and review_scope != "local" and "--root" not in creation:
                return {}
            if historical_pr and (skill != "code-review" or "--promote-pr-run" not in creation):
                return {}
            report = _host_output(output)
            if not report or "\n" in report or "\r" in report:
                return {}
            run = _coordinate(report, command_cwd)
            root = _coordinate(f".reports/codex/{skill}", command_cwd) + "/"
            if historical_pr and "--promote-pr-run" in creation:
                expected_number = historical_pr.rsplit("/", 1)[1]
                if re.fullmatch(rf"pr-{expected_number}/run-[0-9]{{3,}}", run.removeprefix(root)) is None:
                    return {}
            if "--root" in creation:
                promoting = "--promote-pr-run" in creation
                if (
                    command_checkout != review_cwd
                    or checkout is None
                    or checkout.state != "acquired"
                    or not checkout.via_checkout
                    or (skill == "code-review" and not ordinary_slot and owner != "code-remediate")
                ):
                    continue
                # Remediation can resolve storage before queueing. Bind it only at allocation under its acquired PR.
                binding = pr_reports
                if (
                    not binding
                    and early_pr_reports
                    and skill == "code-remediate"
                    and not promoting
                    and not historical_pr
                    and checkout.slot == ordinary_slot
                    and review_scope == early_pr_reports[1]
                    and _report_root_matches(early_pr_reports[0], review_cwd, checkout.slot)
                ):
                    binding = early_pr_reports
                requested_root = _coordinate(creation["--root"], command_cwd)
                # A later turn can reuse a previous root without its lookup receipt. The original writer and
                # target plus a canonical main-checkout allocation justify recovering that missing proof only.
                if (
                    not binding
                    and not report_lookup_seen
                    and not owner
                    and admitted_pr
                    and skill == "code-remediate"
                    and not promoting
                    and creation.get("--pr-number") == review_scope.rsplit("/", 1)[1]
                    and _slot_common(ordinary_slot) == review_cwd + "/.git"
                    and requested_root == review_cwd + "/.git/codex-rig/reports"
                    and run.rsplit("/", 1)[0] == requested_root + "/code-remediate"
                    and any(
                        requester == checkout.requester and _slot_common(slot) == _slot_common(ordinary_slot)
                        for requester, slot in writer_claims
                    )
                ):
                    if unconfirmed_remediation and unconfirmed_remediation != run:
                        return {}
                    unconfirmed_remediation = run
                if not binding or requested_root != binding[0]:
                    continue
                base, url = binding
                number = url.rsplit("/", 1)[1]
                if not promoting and (
                    creation.get("--pr-number") != number or creation.get("--pr-url", url).lower() != url
                ):
                    continue
                root = f"{base}/{skill}/"
                relative_run = run.removeprefix(root)
                if promoting and not re.fullmatch(rf"pr-{number}/run-[0-9]{{3,}}", relative_run):
                    return {}
                if not promoting and (not relative_run or "/" in relative_run):
                    return {}
                pr_reports = binding
                early_pr_reports = None
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
                previous = _coordinate(creation["--promote-pr-run"], command_cwd)
                if runs.get(previous) != skill:
                    return {}
                del runs[previous]
                if previous == superseded_review:
                    superseded_review = run
            elif skill == "code-review" and superseded_review:
                runs.pop(superseded_review, None)
                superseded_review = ""
            if skill == "code-remediate":
                remediation_pr = (run, number) if number and "--promote-pr-run" not in creation else None
                remediation_source = command_checkout
                terminal_checkout = ""
            runs[run] = skill
            owner = owner or skill
        elif finder:
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
                    run = _coordinate(args[-1], command_cwd)
                    if _coordinate(report, command_cwd) != run:
                        continue
                    # The successful finder verifies the run's repository registration and exact PR. Its report
                    # can live in the invoking worktree's own Git admin directory, including a linked worktree.
                    if (
                        caller_lookup
                        and pr
                        and PR_URL.fullmatch(selection[1])
                        and "/codex-rig/reports/code-review/" in run
                    ):
                        if review_scope and review_scope != selection[1].lower():
                            return {}
                        runs[run] = "code-review"
                        owner = owner or "code-review"
                        continue
                    workspace, separator, _ = run.rpartition("/.reports/codex/code-review/")
                    if (
                        caller_lookup
                        and pr
                        and PR_URL.fullmatch(selection[1])
                        and separator
                        and HISTORICAL_ROOT.search(workspace)
                    ):
                        if historical_pr and (historical_pr != selection[1].lower() or review_cwd != workspace):
                            return {}
                        if not historical_pr and (owner or checkout_binding):
                            continue
                        review_cwd, historical_pr = workspace, selection[1].lower()
                    roots = tuple(
                        _coordinate(f".reports/codex/{name}", base) + "/"
                        for base in ({command_cwd, review_cwd} if command_checkout == review_cwd else {review_cwd})
                        for name in ("code-review", "review")
                    )
                    if run.startswith(roots) and (not historical_pr or (pr and selection[1].lower() == historical_pr)):
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
                    run = _coordinate(completion["--complete-run"], command_cwd)
                    if runs.get(run) == "code-review" and run != superseded_review:
                        del runs[run]
        elif helper == _coordinate("shared/remediation_finalize.py", plugin_root):
            if args[2:3] != ["finalize"] or "--promote" not in args or args.count("--run") != 1:
                continue
            index = args.index("--run") + 1
            if index >= len(args):
                return {}
            run = _coordinate(args[index], command_cwd)
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
    if (owner == "code-remediate" or unconfirmed_remediation) and (
        terminal_block.strip("`* ").startswith(REMEDIATION_QUESTIONS)
        or len(set(REMEDIATION_MENU_OPTION.findall(terminal_block.lower()))) >= 2
    ):
        return {}
    checkout_state = checkout.state if checkout is not None else ""
    if checkout_state == "stop" or (
        checkout_state not in {"peer", "available"} and CONTROLLING_STOP.search(stop_prose)
    ):
        return {}
    if selection_status and (
        owner != "code-remediate"
        or owner not in runs.values()
        or terminal_checkout
        or checkout_state in {"peer", "available"}
        or scope_answered
        or TERMINAL_QUESTION.search(terminal_block)
        # Selection status claims an omitted question, so a question or menu delivered anywhere ends the turn.
        or any(question.lower() in message.lower() for question in REMEDIATION_QUESTIONS)
        or len(set(REMEDIATION_MENU_OPTION.findall(message.lower()))) >= 2
    ):
        return {}
    if selection_status and not delivery_only:
        return {
            "decision": "block",
            "reason": (
                f"Recover remediation scope question delivery only. {CONTROLLING_PAUSE} "
                "Follow intake's checkout and PR state requirements before dependent work. Finish reconciling the "
                "authorized findings inventory and present the scope question with its actionable choices. "
                "Keep selection and implementation pending until an explicit answer."
            ),
        }
    writer_held = (
        owner == "code-remediate"
        and checkout_binding is not None
        and any(
            requester == checkout_binding.requester and _slot_common(slot) == _slot_common(checkout_binding.slot)
            for requester, slot in writer_claims
        )
    )
    writer_cleanup = (
        "After all writers have stopped, release the matching writer claim with "
        "review_checkout_slot.py release --slot <retained writer locator> --owner <original owner ID>. "
        if writer_held
        else ""
    )
    if writer_held and (owner not in runs.values() or terminal_checkout in {"released", "released-collision"}):
        if delivery_only or TERMINAL_QUESTION.search(terminal_block):
            return {}
        other_claim = (
            "Release the remaining checkout claim by its own locator. " if checkout_state == "acquired" else ""
        )
        return {
            "decision": "block",
            "reason": f"The in-place writer claim remains active. {CONTROLLING_PAUSE} {other_claim}{writer_cleanup}".rstrip(),
        }
    if terminal_checkout == "released-collision" and remediation_pr and runs == {remediation_pr[0]: "code-remediate"}:
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
    if checkout_state == "available" and not owner:
        return {
            "decision": "block",
            "reason": (
                f"The peer released the checkout before admission. {CONTROLLING_PAUSE} "
                "Use the retained locator and original owner ID to acquire the slot. If a queue process is still "
                "running, monitor that same process until it returns its terminal acquisition receipt."
            ),
        }
    if checkout_state == "acquired" and owner in {"code-review", "code-remediate"} and owner not in runs.values():
        return {
            "decision": "block",
            "reason": (
                f"The {owner} workflow completed while its checkout claim remained active. {CONTROLLING_PAUSE} "
                "Finish authorized worktree cleanup and release only the matching claim with "
                "review_checkout_slot.py release --slot <retained locator> --owner <original owner ID>. "
                "Then deliver the validated report."
            ),
        }
    if checkout_state == "peer":
        return {
            "decision": "block",
            "reason": (
                "The last checkout admission receipt reported a peer claim. This one-time diagnostic grants no "
                f"authority. {CONTROLLING_PAUSE} Honor any newer slot status. If acquisition was interrupted and its "
                "outcome is unknown, check `review_checkout_slot.py status --slot <retained locator>` first. While "
                "still queued, monitor the same cancellable `--queue` acquisition under the original owner ID. A peer's "
                "inspection hold is progress. "
                "If the host needs bounded waits, use `acquire --slot <retained locator> --owner <original owner ID> "
                "--wait-seconds 60`. Preserve the peer's claim. After acquisition, refresh PR state when applicable "
                "and resume the workflow."
            ),
        }
    if unconfirmed_remediation and not owner:
        return {
            "decision": "block",
            "reason": (
                f"The remediation allocation reused report storage without a current confirmation. {CONTROLLING_PAUSE} "
                "Confirm the original checkout and PR with review_code_safety.py report-root. Verify the returned "
                "root matches the allocated run. Restore any released claims with the original invocation ID, "
                "writer before PR target, and recheck PR state and source before dependent work. Once verified, "
                "continue remediation. Only with existing explicit +review authorization, finish the prepared review "
                "and return its validated result to the waiting remediation for selection, fixes, verification and delivery."
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
    if (
        terminal_checkout
        and remediation_pr is not None
        and runs == {remediation_pr[0]: "code-remediate"}
        and owner == "code-remediate"
    ):
        if terminal_checkout in {"released", "released-collision"}:
            return {}
        if checkout_state == "acquired":
            followup = "Report the checkout outcome."
            if terminal_checkout == "collision":
                followup = (
                    "Show the local and remote branch differences from checkout-state.json. Identify the local-only "
                    "commits, explain how the user can back up the verified local tip, and show how to try deleting "
                    "only that local branch with git branch -d. Leave deletion to the user. "
                )
            return {
                "decision": "block",
                "reason": (
                    "The remediation collector stopped at fork checkout. Release only the matching checkout claim "
                    f"using its retained slot and owner. {writer_cleanup}{followup}"
                ),
            }
    if owner == "code-remediate":
        next_step = (
            "Continue remediation selection, selected fixes, verification and delivery under the user's existing scope. "
            "Only if the parent already has explicit +review authorization, finish its needed nested review and admit "
            "the exact validated result before returning to remediation. Reuse the recorded scope and commit "
            "preference. Resolve any missing scope through its normal question. Apply the selected-item count rule "
            "and ask any required commit-grouping question before dependent work."
        )
        if PR_URL.fullmatch(review_scope):
            next_step = (
                "If a required claim was released before source work finished, inspect the retained locators and "
                "restore ownership with the original invocation ID. Follow checkout scheduling to acquire the in-place "
                "writer before the PR target. Before dependent writes, verify all required claims, reverify the "
                "delivery checkout and apply PR State Preflight. For verified delivery needing only the handoff, "
                "follow intake's report-only recovery. " + next_step
            )
    else:
        next_step = (
            "Continue the read-only review. Prepare eligible dependencies and continue independent inspection despite "
            "failed tests. Use disposable source copies for autofixing hooks. Finish reviewer reconciliation and the "
            "configured gates and promote the result. For standalone delivery emit successful --complete-run stdout "
            "verbatim, including its validated tables. Disclose any missed blind checkpoint during finalization."
        )
        if released_slot and released_slot == ordinary_slot and PR_URL.fullmatch(review_scope):
            next_step = (
                "Follow checkout scheduling for the next step. For ordinary inspection, reacquire the released PR slot "
                "with its retained locator and original owner ID. "
                "Apply PR State Preflight and reverify the isolated source before resuming inspection. " + next_step
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
