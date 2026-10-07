"""Exercise compact and historical review reader execution receipts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import runpy
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from test_review_prepare import (
    _CONTEXT_READ_CALL,
    HELPER,
    SKILL,
    _assemble,
    _assembly_evidence,
    _partial_dispatch_evidence,
    _prepare,
    _review_inputs,
    _write_jsonl,
)


def _split_windows_command(command: str) -> list[str]:
    """Split a command line the way the Microsoft C runtime does, inverting ``subprocess.list2cmdline``.

    Quotes group an argument and backslashes escape only when they precede a quote.
    """
    args: list[str] = []
    current: list[str] = []
    in_quotes = started = False
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\":
            end = index
            while end < len(command) and command[end] == "\\":
                end += 1
            count = end - index
            if end < len(command) and command[end] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    end += 1
            else:
                current.append("\\" * count)
            started = True
            index = end
            continue
        if char == '"':
            in_quotes = not in_quotes
            started = True
        elif char in " \t" and not in_quotes:
            if started:
                args.append("".join(current))
                current = []
                started = False
        else:
            current.append(char)
            started = True
        index += 1
    if started:
        args.append("".join(current))
    return args


def _split_command(command: str) -> list[str]:
    """Split a serialized command with the quoting rules of the host that built it."""
    return _split_windows_command(command) if os.name == "nt" else shlex.split(command)


def _join_command(argv: list[str]) -> str:
    """Serialize arguments with the quoting rules of the host that will run them."""
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["python", "reader.py", "--plan", "plan.json"], id="plain"),
        pytest.param([r"D:\a\repo\.venv\Scripts\python.exe", r"C:\Reader\review_context.py"], id="windows-paths"),
        pytest.param([r"C:\dir with space\python.exe", "--plan", r"C:\my dir\plan.json"], id="spaces"),
        pytest.param(["say", 'he said "hi"', "", "tail\\"], id="quotes-empty-trailing-backslash"),
        pytest.param(["x", "back\\\\slash", 'two\\\\"quote', r"\\server\share\file"], id="backslash-runs"),
    ],
)
def test_windows_command_splitter_inverts_list2cmdline(argv: list[str]) -> None:
    """Command lines built for native Windows split back into the exact argument list on any host.

    Mutation tests rewrite individual reader arguments, so POSIX shell splitting would strip the backslashes of a
    Windows path and corrupt the rewritten command.
    """
    assert _split_windows_command(subprocess.list2cmdline(argv)) == argv


@pytest.mark.integration
@pytest.mark.parametrize(
    "damage",
    [
        "mixed-streams",
        "extra-output",
        "wrong-command",
        "wrong-exit",
        "missing-failed-command",
        "wrapper-prefix",
        "wrapper-extra",
    ],
)
def test_partial_missing_reader_stdout_requires_exact_command_receipts(tmp_path: Path, damage: str) -> None:
    """Reject native stdout-shaped launch failures without exact no-source command proof."""
    run, home, children, _, _ = _partial_dispatch_evidence(tmp_path, parent_typo=False, failure="missing-reader-stdout")
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text().splitlines()]
    failed = next(row for row in rows if row.get("payload", {}).get("item", {}).get("exit_code") == 2)
    item = failed["payload"]["item"]
    if damage == "mixed-streams":
        item["stderr"] = item["stdout"]
    elif damage == "extra-output":
        item["stdout"] += "Unverified additional bytes.\n"
    elif damage == "wrong-command":
        item["command"] = ["unrelated command"]
    elif damage == "wrapper-prefix":
        item["command"] = ["/bin/bash", "-lc", "printf unrelated-effect", item["command"][0]]
    elif damage == "wrapper-extra":
        item["command"] = ["/bin/bash", "-lc", item["command"][0], "extra-positional"]
    elif damage == "wrong-exit":
        item["exit_code"] = 0
    else:
        rows.remove(failed)
    _write_jsonl(child, rows)
    protected = {path: path.read_bytes() for path in [child, run / "inspection-plan.json", run / "dispatch.json"]}
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare-repair",
            "--out",
            str(run),
            "--codex-home",
            str(home),
            "--role",
            "challenger",
            "--kind",
            "incomplete-dispatch",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "review-inspection-context-command-mismatch:challenger" in result.stderr
    assert not (run / "repair-dispatch.challenger.json").exists()
    assert all(path.read_bytes() == content for path, content in protected.items())


@pytest.mark.integration
@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "shell-operator",
        "substitution",
        "wrong-final-plan",
        "wrong-role",
        "wrong-budget",
        "wrong-stdout",
        "missing-command",
        "second-duplication",
        "extra-statement",
    ],
)
def test_successful_historical_literal_plan_duplication_requires_exact_safe_receipts(
    tmp_path: Path, damage: str
) -> None:
    """Admit one inert historical plan duplication without accepting stdout-preserving command changes."""
    run, home, children = _assembly_evidence(tmp_path)
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text().splitlines()]
    calls = [row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
    commands = []
    for call in calls:
        args = json.loads(call["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])
        output = next(
            row["payload"]["output"][1]["text"]
            for row in rows
            if row.get("payload", {}).get("type") == "custom_tool_call_output"
            and row["payload"]["call_id"] == call["call_id"]
        )
        commands.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": [args["cmd"]],
                        "exit_code": 0,
                        "stdout": output,
                        "stderr": "",
                    },
                },
            }
        )
    indexes = [0, 1] if damage == "second-duplication" else [1]
    for index in indexes:
        args = json.loads(calls[index]["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])
        argv = _split_command(args["cmd"])
        ignored = str(tmp_path / "discarded" / "review_context.py")
        if damage == "substitution":
            ignored += "$(printf_bad)"
        mutated = [*argv[:3], ignored, "--plan", *argv[3:]]
        if damage == "wrong-final-plan":
            mutated[5] = str(tmp_path / "other-plan.json")
        if damage == "wrong-role":
            mutated[7] = "qa-specialist"
        args["cmd"] = _join_command(mutated)
        if damage == "shell-operator":
            args["cmd"] += "; printf unsafe"
        if damage == "wrong-budget":
            args["max_output_tokens"] = 9999
        calls[index]["input"] = (
            '// @exec: {"max_output_tokens": 10000}\nconst r = await tools.exec_command('
            + json.dumps(args, ensure_ascii=False)
            + "); text(r.output);"
        )
        if damage == "extra-statement":
            calls[index]["input"] += ' text("additional");'
        commands[index]["payload"]["item"]["command"] = [args["cmd"]]
        if damage == "wrong-stdout":
            commands[index]["payload"]["item"]["stdout"] += "altered"
    if damage == "missing-command":
        commands.pop(1)
    rows[-1:-1] = commands
    _write_jsonl(child, rows)
    retained = {
        p: p.read_bytes()
        for p in [child, children["qa-specialist"], run / "dispatch.json", run / "inspection-plan.json"]
    }
    assembled = _assemble(run, home)
    assert all(p.read_bytes() == content for p, content in retained.items())
    if damage == "none":
        assert assembled.returncode == 0, assembled.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        assert all(item["selected_attempt"] == 1 and len(item["attempts"]) == 1 for item in manifest["passes"])
    else:
        assert assembled.returncode != 0
        assert "context-read" in assembled.stderr or "context-command" in assembled.stderr


@pytest.mark.integration
def test_new_dispatch_stores_reader_arguments_once_and_keeps_short_page_selectors(tmp_path: Path) -> None:
    """New ordinary work must retain long coordinates only in its first immutable store frame."""
    run = _review_inputs(tmp_path)
    prepared = _prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    dispatch = json.loads((run / "dispatch.json").read_bytes())
    for call in dispatch["calls"]:
        frames = re.findall(r"```javascript\n(.*?)\n```", call["arguments"]["message"], re.DOTALL)
        assert len(frames) > 1
        assert 'store("review-context-' in frames[0]
        assert all('load("review-context-' in frame for frame in frames[1:])
        assert all(
            str(run / "inspection-plan.json") not in frame and "--role" not in frame and "--attempt" not in frame
            for frame in frames[1:]
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "wrong-key",
        "wrong-selector",
        "missing-setup",
        "wrong-budget",
        "wrong-expanded-command",
        "extra-audited-command",
        "missing-command",
        "wrong-output",
        "wrong-receipt",
        "extra-statement",
        "extra-tool",
    ],
)
def test_compact_native_reads_require_exact_setup_selectors_and_expanded_commands(tmp_path: Path, damage: str) -> None:
    """Accept complete compact native evidence while rejecting cache, selector and execution substitutions."""
    run, home, children = _assembly_evidence(tmp_path, compact_reader=True)
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text().splitlines()]
    calls = [row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
    commands = [row for row in rows if row.get("payload", {}).get("item", {}).get("type") == "CommandExecution"]
    receipt = next(
        row["payload"]
        for row in rows
        if row.get("payload", {}).get("type") == "custom_tool_call_output"
        and row["payload"]["call_id"] == calls[1]["call_id"]
    )
    if damage == "wrong-key":
        calls[1]["input"] = re.sub(r"review-context-[0-9a-f]{64}", "review-context-" + "0" * 64, calls[1]["input"])
    elif damage == "wrong-selector":
        calls[1]["input"] = calls[1]["input"].replace(" --page 2", " --page 1")
    elif damage == "missing-setup":
        calls[0]["input"] = calls[1]["input"]
    elif damage == "wrong-budget":
        calls[0]["input"] = calls[0]["input"].replace('"max_output_tokens": 10000', '"max_output_tokens": 9999')
    elif damage == "wrong-expanded-command":
        commands[1]["payload"]["item"]["command"][0] += " --page 3"
    elif damage == "extra-audited-command":
        commands[1]["payload"]["item"]["command"].append("additional command")
    elif damage == "missing-command":
        rows.remove(commands[1])
    elif damage == "wrong-output":
        receipt["output"][1]["text"] += " altered page"
    elif damage == "wrong-receipt":
        receipt["call_id"] = "unrelated-call"
    elif damage == "extra-statement":
        calls[1]["input"] += ' store("unrelated", "mutation");'
    elif damage == "extra-tool":
        rows.insert(
            -1,
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "additional",
                    "input": "text('extra')",
                },
            },
        )
    _write_jsonl(child, rows)
    original = {
        p: p.read_bytes()
        for p in [child, children["qa-specialist"], run / "dispatch.json", run / "inspection-plan.json"]
    }
    assembled = _assemble(run, home)
    assert all(p.read_bytes() == value for p, value in original.items())
    if damage == "none":
        assert assembled.returncode == 0, assembled.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        assert manifest["dispatch_protocol"] == "paged-context-v8"
        assert (
            manifest["context_reader_sha256"] == hashlib.sha256((SKILL / "review_context.py").read_bytes()).hexdigest()
        )
        assert all(item["selected_attempt"] == 1 for item in manifest["passes"])
        summary = json.loads((run / "inspection-summary.json").read_bytes())
        assert summary["actual_mode"] == "parallel"
        assert summary["independence_satisfied"] is True
    else:
        assert assembled.returncode != 0
        assert "context-read" in assembled.stderr or "context-command" in assembled.stderr


def test_compact_reader_keys_bind_context_role_plan_and_attempt(tmp_path: Path) -> None:
    """Stored commands are isolated across attempts and coordinates while expansion keeps one page suffix."""
    spec = importlib.util.spec_from_file_location("compact_key_prepare", HELPER)
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    render = runpy.run_path(str(SKILL / "review_context.py"))["render_read_call"]
    first = render(tmp_path / "inspection-plan.json", "challenger", 1)
    setup = prepare._compact_read_call(first, "a" * 64, 1)
    later = prepare._compact_read_call(first, "a" * 64, 2)
    key = re.search(r"review-context-[0-9a-f]{64}", setup)[0]
    assert key in later
    assert " --page " not in prepare._read_arguments(setup)["cmd"]
    assert (
        prepare._read_arguments(setup)["cmd"] + " --page 2"
        == prepare._read_arguments(render(tmp_path / "inspection-plan.json", "challenger", 1, page=2))["cmd"]
    )
    for changed, digest in [
        (render(tmp_path / "other-plan.json", "challenger", 1), "a" * 64),
        (render(tmp_path / "inspection-plan.json", "qa-specialist", 1), "a" * 64),
        (render(tmp_path / "inspection-plan.json", "challenger", 2), "a" * 64),
        (first, "b" * 64),
    ]:
        assert key not in prepare._compact_read_call(changed, digest, 1)


@pytest.mark.parametrize(
    ("windows", "literal", "accepted"),
    [
        pytest.param(False, "/retained/unused/review_context.py", True, id="posix-literal"),
        pytest.param(True, "D:\\Unused\\review_context.py", True, id="windows-literal"),
        pytest.param(False, "/unused/$(echo_bad)", False, id="command-substitution"),
        pytest.param(False, "/unused/file;echo_bad", False, id="shell-operator"),
        pytest.param(False, "/unused/*", False, id="glob"),
        pytest.param(True, "D:\\Unused\\%PATH%", False, id="windows-expansion"),
        pytest.param(True, "D:\\Unused\\file&echo_bad", False, id="windows-operator"),
        pytest.param(False, "relative/unused", False, id="relative-coordinate"),
    ],
)
def test_historical_duplicate_plan_accepts_only_canonically_serialized_literal_values(
    windows: bool, literal: str, accepted: bool
) -> None:
    """Prove discarded values inert under both declared host grammars without executing dangerous shell syntax."""
    validator = runpy.run_path(str(SKILL / "validate_artifacts.py"))
    canonical = (
        [
            "C:\\Runtime\\python.exe",
            "C:\\Reader\\review_context.py",
            "--plan",
            "D:\\Run\\inspection-plan.json",
            "--role",
            "challenger",
            "--attempt",
            "1",
            "--page",
            "2",
        ]
        if windows
        else [
            "/runtime/python",
            "/reader/review_context.py",
            "--plan",
            "/run/inspection-plan.json",
            "--role",
            "challenger",
            "--attempt",
            "1",
            "--page",
            "2",
        ]
    )
    serialize = subprocess.list2cmdline if windows else shlex.join

    def frame(argv: list[str]) -> str:
        """Build the direct historical reader frame without altering execution controls."""
        return (
            '// @exec: {"max_output_tokens": 10000}\nconst r = await tools.exec_command('
            + json.dumps({"cmd": serialize(argv), "max_output_tokens": 10000})
            + "); text(r.output);"
        )

    expected = frame(canonical)
    actual = frame([*canonical[:3], literal, "--plan", *canonical[3:]])
    proof = validator["_literal_duplicated_plan_command"](actual, expected, windows=windows)
    assert (proof is not None) is accepted
    if accepted:
        assert proof == (actual, serialize([*canonical[:3], literal, "--plan", *canonical[3:]]))


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["pragma", "plan", "reader"])
def test_compact_reader_preserves_bounded_immediate_launch_recovery(tmp_path: Path, failure: str) -> None:
    """Compact frames preserve existing proved host and first-setup launch recovery without omitting pages."""
    run, home, children = _assembly_evidence(tmp_path, compact_reader=True)
    spec = importlib.util.spec_from_file_location("compact_retry_prepare", HELPER)
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text().splitlines()]
    calls = [row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
    index = 1 if failure == "pragma" else 0
    failed = json.loads(json.dumps(calls[index]))
    failed["call_id"] = "compact-launch-rejection"
    if failure == "pragma":
        failed["input"] = failed["input"].replace(
            '// @exec: {"max_output_tokens": 10000}', '// @exec: "max_output_tokens": 10000'
        )
        output = "exec pragma must be valid JSON with supported fields `yield_time_ms` and `max_output_tokens`: trailing characters at line 1 column 20"
    else:
        canonical = _CONTEXT_READ_CALL(run / "inspection-plan.json", "challenger", 1, sys.executable)
        args = prepare._read_arguments(canonical)
        missing = tmp_path / "missing" / ("inspection-plan.json" if failure == "plan" else "review_context.py")
        args["cmd"] = prepare._read_arguments(
            prepare.validator.render_read_call(
                missing if failure == "plan" else run / "inspection-plan.json",
                "challenger",
                1,
                sys.executable,
                reader_path=missing if failure == "reader" else None,
            )
        )["cmd"]
        digest = hashlib.sha256((run / "specialists/challenger-context.md").read_bytes()).hexdigest()
        failed["input"] = prepare._compact_read_call(canonical, digest, 1, arguments=args)
        result = subprocess.run(
            args["cmd"] if os.name == "nt" else shlex.split(args["cmd"]), capture_output=True, check=False
        )
        assert result.returncode == (1 if failure == "plan" else 2)
        diagnostic = (result.stdout + result.stderr).decode()
        output = [
            {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
            {"type": "input_text", "text": diagnostic},
        ]
        command = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "command": [args["cmd"]],
                    "exit_code": result.returncode,
                    "stdout": result.stdout.decode(),
                    "stderr": result.stderr.decode(),
                },
            },
        }
        position = next(
            i for i, row in enumerate(rows) if row.get("payload", {}).get("item", {}).get("type") == "CommandExecution"
        )
        rows.insert(position, command)
    position = next(i for i, row in enumerate(rows) if row.get("payload") == calls[index])
    rows[position:position] = [
        {"type": "response_item", "payload": failed},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": failed["call_id"], "output": output},
        },
    ]
    _write_jsonl(child, rows)
    original = child.read_bytes()
    result = _assemble(run, home)
    assert result.returncode == 0, result.stderr
    assert child.read_bytes() == original
    assert all(
        item["selected_attempt"] == 1 for item in json.loads((run / "specialist-manifest.json").read_bytes())["passes"]
    )


@pytest.mark.integration
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize(
    "wrapper",
    ["normalized", "bash", "prefix-program", "unrelated-executable", "extra-positional", "wrong-flag", "wrong-command"],
)
def test_reader_receipts_require_complete_single_command_wrapper(tmp_path: Path, compact: bool, wrapper: str) -> None:
    """Reject stdout-preserving wrapper substitutions for both historical and compact native reader frames."""
    run, home, children = _assembly_evidence(tmp_path, compact_reader=compact)
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text().splitlines()]
    calls = [row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
    commands = [row for row in rows if row.get("payload", {}).get("item", {}).get("type") == "CommandExecution"]
    if not compact:
        for page, call in enumerate(calls, 1):
            canonical = _CONTEXT_READ_CALL(run / "inspection-plan.json", "challenger", 1, sys.executable, page)
            cmd = json.loads(canonical.split("tools.exec_command(", 1)[1].split("); text", 1)[0])["cmd"]
            output = next(
                row["payload"]["output"][1]["text"]
                for row in rows
                if row.get("payload", {}).get("type") == "custom_tool_call_output"
                and row["payload"]["call_id"] == call["call_id"]
            )
            commands.append(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "CommandExecution",
                            "command": [cmd],
                            "exit_code": 0,
                            "stdout": output,
                            "stderr": "",
                        },
                    },
                }
            )
        rows[-1:-1] = commands
    item = commands[0]["payload"]["item"]
    cmd = item["command"][0]
    item["command"] = [cmd] if wrapper == "normalized" else ["/bin/bash", "-lc", cmd]
    if wrapper == "prefix-program":
        item["command"].insert(2, "printf unrelated-effect")
    elif wrapper == "unrelated-executable":
        item["command"][0] = "/unknown/executable"
    elif wrapper == "extra-positional":
        item["command"].append("extra-positional")
    elif wrapper == "wrong-flag":
        item["command"][1] = "-i"
    elif wrapper == "wrong-command":
        item["command"][-1] = "unrelated command"
    _write_jsonl(child, rows)
    before = {p: p.read_bytes() for p in [child, run / "dispatch.json", run / "inspection-plan.json"]}
    result = _assemble(run, home)
    assert all(p.read_bytes() == data for p, data in before.items())
    if wrapper in {"normalized", "bash"}:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "context-command-mismatch" in result.stderr


@pytest.mark.parametrize(
    ("prefix", "accepted"),
    [
        pytest.param([], True, id="normalized"),
        pytest.param(["/bin/bash", "-lc"], True, id="observed-native-bash"),
        pytest.param(["/bin/bash", "-c"], True, id="bash-command"),
        pytest.param(["/bin/zsh", "-lc"], True, id="zsh-login-command"),
        pytest.param(["/bin/sh", "-c"], True, id="posix-command"),
        pytest.param(["cmd.exe", "/c"], True, id="windows-cmd-command"),
        pytest.param([r"D:\Windows\System32\cmd.exe", "/C"], True, id="windows-absolute-cmd"),
        pytest.param(["powershell.exe", "-Command"], True, id="windows-powershell"),
        pytest.param(
            [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-Command"],
            True,
            id="windows-absolute-powershell",
        ),
        pytest.param(["pwsh.exe", "-command"], True, id="windows-core-powershell"),
        pytest.param(["/bin/bash", "-lc", "different program"], False, id="preceding-program"),
        pytest.param(["/unrelated/bash", "-lc"], False, id="unobserved-executable-coordinate"),
        pytest.param(["/bin/bash", "-i"], False, id="wrong-posix-mode"),
        pytest.param(["/bin/bash", "-lc", "extra-positional"], False, id="extra-prefix-position"),
        pytest.param([r"D:\Unrelated\cmd.exe", "/c"], False, id="unrelated-windows-coordinate"),
        pytest.param(["cmd.exe", "/k"], False, id="persistent-windows-shell"),
        pytest.param(["powershell.exe", "-File"], False, id="windows-file-mode"),
        pytest.param(["pwsh.exe", "-Command", "unrelated"], False, id="extra-windows-position"),
    ],
)
def test_reader_wrapper_grammar_preserves_declared_platform_coordinates(prefix: list[str], accepted: bool) -> None:
    """Validate closed shell semantics independently of the test host without executing any supplied command."""
    validator = runpy.run_path(str(SKILL / "validate_artifacts.py"))
    expected = "reader --plan retained.json --role reviewer --attempt 1"
    assert validator["_reader_command_matches"]([*prefix, expected], expected) is accepted
    assert validator["_reader_command_matches"]([*prefix, expected, "extra-positional"], expected) is False
