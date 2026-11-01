"""Exercise compact and historical review reader execution receipts.

Each scenario damages the native evidence of one assembled review wave (a reader command, its wrapper, its output or its
setup frame) and checks that assembly admits the canonical form and refuses every substitution.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import runpy
import shlex
import subprocess
import sys
from functools import partial
from pathlib import Path

import pytest
from _artifact_files import read_jsonl
from _modules import load_module
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

_EXEC_PRAGMA = '// @exec: {"max_output_tokens": 10000}\nconst r = await tools.exec_command('
_EXEC_TAIL = "); text(r.output);"
_REJECTION_TOKENS = ("context-read", "context-command")
_LAUNCH_REJECTION_CALL_ID = "compact-launch-rejection"
_PRAGMA_REJECTION = (
    "exec pragma must be valid JSON with supported fields `yield_time_ms` and `max_output_tokens`: "
    "trailing characters at line 1 column 20"
)
_POSIX_CANONICAL = [
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
_WINDOWS_CANONICAL = [
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


def _retain(paths: list[Path]) -> dict[Path, bytes]:
    """Snapshot file bytes so a test can prove assembly never rewrites its evidence."""
    return {path: path.read_bytes() for path in paths}


def _reader_calls(rows: list[dict]) -> list[dict]:
    """Return the payloads of every reader tool call in a child session."""
    return [row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]


def _call_output(rows: list[dict], call_id: str) -> dict:
    """Return the tool-call output payload recorded for one reader call."""
    return next(
        row["payload"]
        for row in rows
        if row.get("payload", {}).get("type") == "custom_tool_call_output" and row["payload"]["call_id"] == call_id
    )


def _exec_arguments(frame: str) -> dict:
    """Parse the ``tools.exec_command`` arguments out of a serialized reader frame."""
    return json.loads(frame.split("tools.exec_command(", 1)[1].split("); text", 1)[0])


def _reader_frame(arguments: dict) -> str:
    """Build the direct historical reader frame around serialized execution arguments."""
    return _EXEC_PRAGMA + json.dumps(arguments, ensure_ascii=False) + _EXEC_TAIL


def _command_row(command: str, stdout: str) -> dict:
    """Build the audited native ``CommandExecution`` event that matches one reader call."""
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {"type": "CommandExecution", "command": [command], "exit_code": 0, "stdout": stdout, "stderr": ""},
        },
    }


def _native_command_rows(rows: list[dict]) -> list[dict]:
    """Return the audited command events already recorded in a compact child session."""
    return [row for row in rows if row.get("payload", {}).get("item", {}).get("type") == "CommandExecution"]


def _historical_command_rows(run: Path, rows: list[dict], calls: list[dict]) -> list[dict]:
    """Record canonical audited command events for every historical reader call before the terminal row."""
    commands = [
        _command_row(
            _exec_arguments(_CONTEXT_READ_CALL(run / "inspection-plan.json", "challenger", 1, sys.executable, page))[
                "cmd"
            ],
            _call_output(rows, call["call_id"])["output"][1]["text"],
        )
        for page, call in enumerate(calls, 1)
    ]
    rows[-1:-1] = commands
    return commands


def _javascript_frames(message: str) -> list[str]:
    """Collect the bodies of the fenced ``javascript`` frames in a dispatch message, line by line."""
    frames: list[str] = []
    current: list[str] | None = None
    for line in message.splitlines():
        if current is None:
            current = [] if line.startswith("```javascript") else None
        elif line.startswith("```"):
            frames.append("\n".join(current))
            current = None
        else:
            current.append(line)
    return frames


def _prepare_repair(run: Path, home: Path, role: str, kind: str) -> subprocess.CompletedProcess[str]:
    """Run the real ``prepare-repair`` command for one role and recovery kind."""
    argv = [sys.executable, str(HELPER), "prepare-repair", "--out", str(run), "--codex-home", str(home)]
    return subprocess.run(
        [*argv, "--role", role, "--kind", kind],
        capture_output=True,
        text=True,
        check=False,
    )


def _mixed_streams(item: dict, rows: list[dict], failed: dict) -> None:
    """Report the stdout bytes on stderr as well."""
    item["stderr"] = item["stdout"]


def _extra_output(item: dict, rows: list[dict], failed: dict) -> None:
    """Append bytes the reader never produced."""
    item["stdout"] += "Unverified additional bytes.\n"


def _wrong_command(item: dict, rows: list[dict], failed: dict) -> None:
    """Replace the audited command with an unrelated one."""
    item["command"] = ["unrelated command"]


def _wrong_exit(item: dict, rows: list[dict], failed: dict) -> None:
    """Report a successful exit for a failed launch."""
    item["exit_code"] = 0


def _wrapper_prefix(item: dict, rows: list[dict], failed: dict) -> None:
    """Run an unrelated program before the reader inside the shell wrapper."""
    item["command"] = ["/bin/bash", "-lc", "printf unrelated-effect", item["command"][0]]


def _wrapper_extra(item: dict, rows: list[dict], failed: dict) -> None:
    """Pass an extra positional argument to the shell wrapper."""
    item["command"] = ["/bin/bash", "-lc", item["command"][0], "extra-positional"]


def _drop_failed_command(item: dict, rows: list[dict], failed: dict) -> None:
    """Drop the failed command event entirely, leaving the launch without a command receipt."""
    rows.remove(failed)


def _duplicated_plan_run(
    tmp_path: Path,
    *,
    indexes: tuple[int, ...] = (1,),
    ignored_suffix: str = "",
    plan_file: str | None = None,
    role: str | None = None,
    cmd_suffix: str = "",
    extra_args: dict | None = None,
    statement_suffix: str = "",
    stdout_suffix: str = "",
    dropped: tuple[int, int] = (0, 0),
) -> tuple[Path, Path, dict[Path, bytes]]:
    """Duplicate the literal ``--plan`` option in the chosen historical reader calls and record matching commands."""
    run, home, children = _assembly_evidence(tmp_path)
    child = children["challenger"]
    rows = read_jsonl(child)
    calls = _reader_calls(rows)
    commands = [
        _command_row(_exec_arguments(call["input"])["cmd"], _call_output(rows, call["call_id"])["output"][1]["text"])
        for call in calls
    ]
    for index in indexes:
        args = _exec_arguments(calls[index]["input"])
        argv = _split_command(args["cmd"])
        ignored = str(tmp_path / "discarded" / "review_context.py") + ignored_suffix
        mutated = [*argv[:3], ignored, "--plan", *argv[3:]]
        mutated[5] = str(tmp_path / plan_file) if plan_file else mutated[5]
        mutated[7] = role or mutated[7]
        args["cmd"] = _join_command(mutated) + cmd_suffix
        args.update(extra_args or {})
        calls[index]["input"] = _reader_frame(args) + statement_suffix
        item = commands[index]["payload"]["item"]
        item["command"] = [args["cmd"]]
        item["stdout"] += stdout_suffix
    del commands[slice(*dropped)]
    rows[-1:-1] = commands
    _write_jsonl(child, rows)
    retained = _retain([child, children["qa-specialist"], run / "dispatch.json", run / "inspection-plan.json"])
    return run, home, retained


def _wrong_key(rows: list[dict], calls: list[dict], commands: list[dict], receipt: dict) -> None:
    """Bind the stored command to a cache key that no setup frame created."""
    calls[1]["input"] = re.sub(r"review-context-[0-9a-f]{64}", "review-context-" + "0" * 64, calls[1]["input"])


def _wrong_selector(rows: list[dict], calls: list[dict], commands: list[dict], receipt: dict) -> None:
    """Load a different page than the audited command expands."""
    calls[1]["input"] = calls[1]["input"].replace(" --page 2", " --page 1")


def _missing_setup(rows: list[dict], calls: list[dict], commands: list[dict], receipt: dict) -> None:
    """Replace the setup frame with a second load frame."""
    calls[0]["input"] = calls[1]["input"]


def _wrong_budget(rows: list[dict], calls: list[dict], commands: list[dict], receipt: dict) -> None:
    """Lower the output budget of the setup frame."""
    calls[0]["input"] = calls[0]["input"].replace('"max_output_tokens": 10000', '"max_output_tokens": 9999')


def _wrong_expanded_command(rows: list[dict], calls: list[dict], commands: list[dict], receipt: dict) -> None:
    """Expand the stored command to a different page."""
    commands[1]["payload"]["item"]["command"][0] += " --page 3"


def _extra_audited_command(rows: list[dict], calls: list[dict], commands: list[dict], receipt: dict) -> None:
    """Add a second command to one audited execution."""
    commands[1]["payload"]["item"]["command"].append("additional command")


def _missing_command(rows: list[dict], calls: list[dict], commands: list[dict], receipt: dict) -> None:
    """Remove the audited command event of one page."""
    rows.remove(commands[1])


def _wrong_output(rows: list[dict], calls: list[dict], commands: list[dict], receipt: dict) -> None:
    """Alter the page text the reader returned."""
    receipt["output"][1]["text"] += " altered page"


def _wrong_receipt(rows: list[dict], calls: list[dict], commands: list[dict], receipt: dict) -> None:
    """Point the page output at a call that never ran."""
    receipt["call_id"] = "unrelated-call"


def _extra_statement(rows: list[dict], calls: list[dict], commands: list[dict], receipt: dict) -> None:
    """Append an unrelated store statement to the load frame."""
    calls[1]["input"] += ' store("unrelated", "mutation");'


def _extra_tool(rows: list[dict], calls: list[dict], commands: list[dict], receipt: dict) -> None:
    """Insert an unrelated tool call before the terminal row."""
    rows.insert(
        -1,
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "additional", "input": "text('extra')"},
        },
    )


def _undamaged(rows: list[dict], calls: list[dict], commands: list[dict], receipt: dict) -> None:
    """Leave the compact native evidence canonical."""


def _compact_native_run(tmp_path: Path, damage) -> tuple[Path, Path, dict[Path, bytes]]:
    """Apply one damage to the compact challenger session and snapshot every evidence file."""
    run, home, children = _assembly_evidence(tmp_path, compact_reader=True)
    child = children["challenger"]
    rows = read_jsonl(child)
    calls = _reader_calls(rows)
    damage(rows, calls, _native_command_rows(rows), _call_output(rows, calls[1]["call_id"]))
    _write_jsonl(child, rows)
    retained = _retain([child, children["qa-specialist"], run / "dispatch.json", run / "inspection-plan.json"])
    return run, home, retained


def _pragma_launch_failure(run: Path, tmp_path: Path, rows: list[dict], calls: list[dict]):
    """Describe a rejected load frame whose exec pragma is not valid JSON."""
    failed = copy.deepcopy(calls[1])
    failed["call_id"] = _LAUNCH_REJECTION_CALL_ID
    failed["input"] = failed["input"].replace(
        '// @exec: {"max_output_tokens": 10000}', '// @exec: "max_output_tokens": 10000'
    )
    return 1, failed, _PRAGMA_REJECTION


def _missing_path_launch_failure(
    missing_name: str, exit_code: int, run: Path, tmp_path: Path, rows: list[dict], calls: list[dict]
):
    """Run the real reader against a missing plan or reader file and record its diagnostic as the rejected setup."""
    prepare = load_module("compact_retry_prepare", HELPER)
    plan_missing = missing_name == "inspection-plan.json"
    missing = tmp_path / "missing" / missing_name
    canonical = _CONTEXT_READ_CALL(run / "inspection-plan.json", "challenger", 1, sys.executable)
    args = prepare._read_arguments(canonical)
    rendered = prepare.validator.render_read_call(
        missing if plan_missing else run / "inspection-plan.json",
        "challenger",
        1,
        sys.executable,
        reader_path=None if plan_missing else missing,
    )
    args["cmd"] = prepare._read_arguments(rendered)["cmd"]
    digest = hashlib.sha256((run / "specialists/challenger-context.md").read_bytes()).hexdigest()
    failed = copy.deepcopy(calls[0])
    failed["call_id"] = _LAUNCH_REJECTION_CALL_ID
    failed["input"] = prepare._compact_read_call(canonical, digest, 1, arguments=args)
    result = subprocess.run(
        args["cmd"] if os.name == "nt" else shlex.split(args["cmd"]), capture_output=True, check=False
    )
    assert result.returncode == exit_code
    output = [
        {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
        {"type": "input_text", "text": (result.stdout + result.stderr).decode()},
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
    first_command = next(
        i for i, row in enumerate(rows) if row.get("payload", {}).get("item", {}).get("type") == "CommandExecution"
    )
    rows.insert(first_command, command)
    return 0, failed, output


def _wrapped_command_run(tmp_path: Path, compact: bool, wrap) -> tuple[Path, Path, dict[Path, bytes]]:
    """Rewrite the first audited challenger command through ``wrap`` and snapshot the evidence files."""
    run, home, children = _assembly_evidence(tmp_path, compact_reader=compact)
    child = children["challenger"]
    rows = read_jsonl(child)
    calls = _reader_calls(rows)
    commands = _native_command_rows(rows) if compact else _historical_command_rows(run, rows, calls)
    item = commands[0]["payload"]["item"]
    item["command"] = wrap(item["command"][0])
    _write_jsonl(child, rows)
    return run, home, _retain([child, run / "dispatch.json", run / "inspection-plan.json"])


class TestWindowsCommandSplitter:
    """The test-side inverse of ``subprocess.list2cmdline`` used to rewrite reader arguments on any host."""

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
    def test_inverts_list2cmdline(self, argv: list[str]) -> None:
        """Command lines built for native Windows split back into the exact argument list on any host.

        Mutation tests rewrite individual reader arguments, so POSIX shell splitting would strip the backslashes of a
        Windows path and corrupt the rewritten command.
        """
        assert _split_windows_command(subprocess.list2cmdline(argv)) == argv


class TestPartialMissingReaderStdout:
    """Native stdout-shaped launch failures need exact no-source command proof before a repair is prepared."""

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_mixed_streams, id="mixed-streams"),
            pytest.param(_extra_output, id="extra-output"),
            pytest.param(_wrong_command, id="wrong-command"),
            pytest.param(_wrong_exit, id="wrong-exit"),
            pytest.param(_drop_failed_command, id="missing-failed-command"),
            pytest.param(_wrapper_prefix, id="wrapper-prefix"),
            pytest.param(_wrapper_extra, id="wrapper-extra"),
        ],
    )
    def test_rejects_launch_failure_without_exact_receipt(self, tmp_path: Path, damage) -> None:
        """Reject a repair for a launch failure whose command receipt is altered, wrapped or missing.

        A challenger session ends in a reader launch failure with no source output. Each case damages that failure's
        audited command; none may yield a repair dispatch, and no evidence file may be rewritten.
        """
        run, home, children, _, _ = _partial_dispatch_evidence(
            tmp_path, parent_typo=False, failure="missing-reader-stdout"
        )
        child = children["challenger"]
        rows = read_jsonl(child)
        failed = next(row for row in rows if row.get("payload", {}).get("item", {}).get("exit_code") == 2)
        damage(failed["payload"]["item"], rows, failed)
        _write_jsonl(child, rows)
        retained = _retain([child, run / "inspection-plan.json", run / "dispatch.json"])
        result = _prepare_repair(run, home, "challenger", "incomplete-dispatch")
        assert result.returncode != 0
        assert "review-inspection-context-command-mismatch:challenger" in result.stderr
        assert not (run / "repair-dispatch.challenger.json").exists()
        assert all(path.read_bytes() == content for path, content in retained.items())


class TestHistoricalPlanDuplication:
    """A historical reader call may repeat ``--plan`` once with an inert literal; nothing else may change."""

    @pytest.mark.integration
    def test_admits_one_inert_duplication(self, tmp_path: Path) -> None:
        """Admit a single historical reader call whose discarded ``--plan`` literal is inert and fully receipted.

        The second page call repeats ``--plan`` after a throw-away reader path, with the audited command and stdout
        matching the serialized call, so assembly must still select the first attempt of every role.
        """
        run, home, retained = _duplicated_plan_run(tmp_path)
        assembled = _assemble(run, home)
        assert all(path.read_bytes() == content for path, content in retained.items())
        assert assembled.returncode == 0, assembled.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        assert all(item["selected_attempt"] == 1 and len(item["attempts"]) == 1 for item in manifest["passes"])

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "options",
        [
            pytest.param({"cmd_suffix": "; printf unsafe"}, id="shell-operator"),
            pytest.param({"ignored_suffix": "$(printf_bad)"}, id="substitution"),
            pytest.param({"plan_file": "other-plan.json"}, id="wrong-final-plan"),
            pytest.param({"role": "qa-specialist"}, id="wrong-role"),
            pytest.param({"extra_args": {"max_output_tokens": 9999}}, id="wrong-budget"),
            pytest.param({"stdout_suffix": "altered"}, id="wrong-stdout"),
            pytest.param({"dropped": (1, 2)}, id="missing-command"),
            pytest.param({"indexes": (0, 1)}, id="second-duplication"),
            pytest.param({"statement_suffix": ' text("additional");'}, id="extra-statement"),
        ],
    )
    def test_rejects_stdout_preserving_command_change(self, tmp_path: Path, options: dict) -> None:
        """Refuse a duplicated plan whose command, budget, output or call statements differ from the receipts.

        Each case keeps the reader stdout intact yet changes one part of the serialized call or its audited command,
        which the exact-receipt comparison must catch; the evidence files stay byte-identical either way.
        """
        run, home, retained = _duplicated_plan_run(tmp_path, **options)
        assembled = _assemble(run, home)
        assert all(path.read_bytes() == content for path, content in retained.items())
        assert assembled.returncode != 0
        assert any(token in assembled.stderr for token in _REJECTION_TOKENS)

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
    def test_accepts_only_canonically_serialized_literals(self, windows: bool, literal: str, accepted: bool) -> None:
        """Prove discarded values inert under both declared host grammars without executing shell syntax.

        The validator parses the duplicated command under POSIX or Windows quoting and accepts only a plain absolute
        literal; substitutions, operators, globs, variable expansion and relative coordinates are refused.
        """
        validator = runpy.run_path(str(SKILL / "validate_artifacts.py"))
        canonical = _WINDOWS_CANONICAL if windows else _POSIX_CANONICAL
        serialize = subprocess.list2cmdline if windows else shlex.join
        duplicated = [*canonical[:3], literal, "--plan", *canonical[3:]]
        expected = _reader_frame({"cmd": serialize(canonical), "max_output_tokens": 10000})
        actual = _reader_frame({"cmd": serialize(duplicated), "max_output_tokens": 10000})
        proof = validator["_literal_duplicated_plan_command"](actual, expected, windows=windows)
        assert (proof is not None) is accepted
        assert proof == ((actual, serialize(duplicated)) if accepted else None)


class TestCompactReader:
    """Compact native reader frames store coordinates once and expand them to exact audited commands."""

    @pytest.mark.integration
    def test_new_dispatch_stores_arguments_once(self, tmp_path: Path) -> None:
        """New ordinary work keeps long coordinates only in its first immutable store frame.

        Every later page frame loads the stored command by a short selector, so the plan path, role and attempt flags
        must not be repeated outside the first frame of each dispatched call.
        """
        run = _review_inputs(tmp_path)
        prepared = _prepare(run)
        assert prepared.returncode == 0, prepared.stderr
        dispatch = json.loads((run / "dispatch.json").read_bytes())
        per_call = [_javascript_frames(call["arguments"]["message"]) for call in dispatch["calls"]]
        later = [frame for frames in per_call for frame in frames[1:]]
        assert all(len(frames) > 1 for frames in per_call)
        assert all('store("review-context-' in frames[0] for frames in per_call)
        assert all('load("review-context-' in frame for frame in later)
        assert all(
            str(run / "inspection-plan.json") not in frame and "--role" not in frame and "--attempt" not in frame
            for frame in later
        )

    @pytest.mark.integration
    def test_admits_complete_native_reads(self, tmp_path: Path) -> None:
        """Accept complete compact native evidence and bind the manifest to the reader that produced it.

        A canonical compact wave carries the setup frame, every load selector, the expanded commands and exact page
        outputs; assembly must record the paged protocol and the reader hash and report a parallel independent run.
        """
        run, home, retained = _compact_native_run(tmp_path, _undamaged)
        assembled = _assemble(run, home)
        assert all(path.read_bytes() == value for path, value in retained.items())
        assert assembled.returncode == 0, assembled.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        summary = json.loads((run / "inspection-summary.json").read_bytes())
        assert manifest["dispatch_protocol"] == "paged-context-v8"
        assert (
            manifest["context_reader_sha256"] == hashlib.sha256((SKILL / "review_context.py").read_bytes()).hexdigest()
        )
        assert all(item["selected_attempt"] == 1 for item in manifest["passes"])
        assert summary["actual_mode"] == "parallel"
        assert summary["independence_satisfied"] is True

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_wrong_key, id="wrong-key"),
            pytest.param(_wrong_selector, id="wrong-selector"),
            pytest.param(_missing_setup, id="missing-setup"),
            pytest.param(_wrong_budget, id="wrong-budget"),
            pytest.param(_wrong_expanded_command, id="wrong-expanded-command"),
            pytest.param(_extra_audited_command, id="extra-audited-command"),
            pytest.param(_missing_command, id="missing-command"),
            pytest.param(_wrong_output, id="wrong-output"),
            pytest.param(_wrong_receipt, id="wrong-receipt"),
            pytest.param(_extra_statement, id="extra-statement"),
            pytest.param(_extra_tool, id="extra-tool"),
        ],
    )
    def test_rejects_substituted_setup_selector_or_command(self, tmp_path: Path, damage) -> None:
        """Refuse compact native evidence whose cache key, selector, budget, command or output was substituted.

        Each case damages one part of an otherwise canonical compact wave; assembly must name a context-read or context-
        command mismatch and leave every evidence file untouched.
        """
        run, home, retained = _compact_native_run(tmp_path, damage)
        assembled = _assemble(run, home)
        assert all(path.read_bytes() == value for path, value in retained.items())
        assert assembled.returncode != 0
        assert any(token in assembled.stderr for token in _REJECTION_TOKENS)

    def test_keys_bind_context_role_plan_and_attempt(self, tmp_path: Path) -> None:
        """Stored commands are isolated across attempts and coordinates while expansion keeps one page suffix.

        The cache key of a setup frame must be reused by later attempts of the same coordinates, and any change of plan,
        role, attempt or context digest must produce a different key.
        """
        prepare = load_module("compact_key_prepare", HELPER)
        render = runpy.run_path(str(SKILL / "review_context.py"))["render_read_call"]
        first = render(tmp_path / "inspection-plan.json", "challenger", 1)
        setup = prepare._compact_read_call(first, "a" * 64, 1)
        later = prepare._compact_read_call(first, "a" * 64, 2)
        key = re.search(r"review-context-[0-9a-f]{64}", setup)[0]
        paged = render(tmp_path / "inspection-plan.json", "challenger", 1, page=2)
        changed = [
            (render(tmp_path / "other-plan.json", "challenger", 1), "a" * 64),
            (render(tmp_path / "inspection-plan.json", "qa-specialist", 1), "a" * 64),
            (render(tmp_path / "inspection-plan.json", "challenger", 2), "a" * 64),
            (first, "b" * 64),
        ]
        assert key in later
        assert " --page " not in prepare._read_arguments(setup)["cmd"]
        assert prepare._read_arguments(setup)["cmd"] + " --page 2" == prepare._read_arguments(paged)["cmd"]
        assert all(key not in prepare._compact_read_call(call, digest, 1) for call, digest in changed)

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "record_failure",
        [
            pytest.param(_pragma_launch_failure, id="pragma"),
            pytest.param(partial(_missing_path_launch_failure, "inspection-plan.json", 1), id="plan"),
            pytest.param(partial(_missing_path_launch_failure, "review_context.py", 2), id="reader"),
        ],
    )
    def test_preserves_bounded_immediate_launch_recovery(self, tmp_path: Path, record_failure) -> None:
        """Compact frames keep proved host and first-setup launch recovery without omitting pages.

        A rejected pragma, missing plan or missing reader precedes the retried call. The proved rejection must be
        accepted as bounded recovery, leaving the child session untouched and every role on its first attempt.
        """
        run, home, children = _assembly_evidence(tmp_path, compact_reader=True)
        child = children["challenger"]
        rows = read_jsonl(child)
        calls = _reader_calls(rows)
        index, failed, output = record_failure(run, tmp_path, rows, calls)
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
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        assert result.returncode == 0, result.stderr
        assert child.read_bytes() == original
        assert all(item["selected_attempt"] == 1 for item in manifest["passes"])


class TestReaderCommandWrapper:
    """Audited reader commands may be wrapped only by the declared shell grammar around the exact reader command."""

    @pytest.mark.integration
    @pytest.mark.parametrize("compact", [False, True])
    @pytest.mark.parametrize(
        "wrap",
        [
            pytest.param(lambda cmd: [cmd], id="normalized"),
            pytest.param(lambda cmd: ["/bin/bash", "-lc", cmd], id="bash"),
        ],
    )
    def test_admits_normalized_and_bash_wrappers(self, tmp_path: Path, compact: bool, wrap) -> None:
        """Admit the bare command and the observed native bash login wrapper for historical and compact readers.

        The audited command of the first page is recorded either unwrapped or as the bash login wrapper around the exact
        reader command; both are the declared forms, so assembly succeeds without rewriting evidence.
        """
        run, home, before = _wrapped_command_run(tmp_path, compact, wrap)
        result = _assemble(run, home)
        assert all(path.read_bytes() == data for path, data in before.items())
        assert result.returncode == 0, result.stderr

    @pytest.mark.integration
    @pytest.mark.parametrize("compact", [False, True])
    @pytest.mark.parametrize(
        "wrap",
        [
            pytest.param(lambda cmd: ["/bin/bash", "-lc", "printf unrelated-effect", cmd], id="prefix-program"),
            pytest.param(lambda cmd: ["/unknown/executable", "-lc", cmd], id="unrelated-executable"),
            pytest.param(lambda cmd: ["/bin/bash", "-lc", cmd, "extra-positional"], id="extra-positional"),
            pytest.param(lambda cmd: ["/bin/bash", "-i", cmd], id="wrong-flag"),
            pytest.param(lambda cmd: ["/bin/bash", "-lc", "unrelated command"], id="wrong-command"),
        ],
    )
    def test_rejects_stdout_preserving_wrapper_substitution(self, tmp_path: Path, compact: bool, wrap) -> None:
        """Reject wrappers that keep the reader stdout but run another program, executable, flag or extra argument.

        Both the historical and compact native reader frames record the wrapped command for the first page; the wrapper
        must be complete and single-command, so every substitution reports a context-command mismatch.
        """
        run, home, before = _wrapped_command_run(tmp_path, compact, wrap)
        result = _assemble(run, home)
        assert all(path.read_bytes() == data for path, data in before.items())
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
    def test_grammar_preserves_declared_platform_coordinates(self, prefix: list[str], accepted: bool) -> None:
        """Validate closed shell semantics independently of the test host without executing any supplied command.

        The wrapper matcher is a pure function of the argument list, so POSIX and Windows shells are judged by the
        declared coordinates alone and any trailing positional argument is refused.
        """
        validator = runpy.run_path(str(SKILL / "validate_artifacts.py"))
        expected = "reader --plan retained.json --role reviewer --attempt 1"
        assert validator["_reader_command_matches"]([*prefix, expected], expected) is accepted
        assert validator["_reader_command_matches"]([*prefix, expected, "extra-positional"], expected) is False
