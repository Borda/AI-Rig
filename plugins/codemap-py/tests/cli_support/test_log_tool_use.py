"""Contract test: log-tool-use.py appends Claude-scoped tool telemetry records.

The PostToolUse hook (`log-tool-use.py`) is the raw grep/read-volume signal codemap's
index-hygiene fixes aim to reduce. It must:

- write one JSON line to `tools_<session>.jsonl` for each Grep/Read/Glob call, carrying
  `tool` + the right `target` field (Grep/Glob pattern-or-path, Read file_path);
- write beneath the `claude/` runtime directory and join on the same session key as the
  CLI shard (`tools_<session>.jsonl` when seeded, unsuffixed `tools.jsonl` otherwise);
- never read `tool_response` — parsing search/read output is the exact cost the hook must
  not pay (accept criterion + <5ms budget), so a hostile non-JSON tool_response must not
  change behaviour;
- honour `CODEMAP_LOGGING=false` (mirrors `_telemetry.py`'s env gate) and fail open;
- keep the repeated-read nudge bounded: it runs on every matched Read, so it inspects a
  fixed tail of the shard instead of the whole 10 MB rotation budget.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import join_avoidance as ja
import pytest

_HOOK = Path(__file__).parent.parent.parent / "hooks" / "log-tool-use.py"

_SPEC = importlib.util.spec_from_file_location("codemap_log_tool_use", _HOOK)
assert _SPEC
assert _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _run(payload: dict, cwd: Path, *, logging: str | None = None) -> subprocess.CompletedProcess:
    """Feed one PostToolUse event through the hook with cwd + env isolated to a tmp dir."""
    env = {**os.environ}
    # Force the log dir under cwd; unset any inherited override so tests are hermetic.
    env.pop("CODEMAP_LOG_DIR", None)
    # conftest's autouse _telemetry_off exports CODEMAP_LOGGING=false suite-wide;
    # this helper's default must re-enable it or every write-asserting case goes dark.
    env["CODEMAP_LOGGING"] = logging if logging is not None else "true"
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(cwd),
        env=env,
    )


def _read_records(cwd: Path) -> list[dict]:
    """Return all records across every tools*.jsonl shard under the cwd log dir."""
    log_dir = cwd / ".cache" / "codemap" / "logs" / "claude"
    records: list[dict] = []
    for shard in sorted(log_dir.glob("tools*.jsonl")):
        records += [json.loads(line) for line in shard.read_text().splitlines() if line.strip()]
    return records


@pytest.mark.parametrize(
    ("tool", "tool_input", "expected_target", "expected_search_path"),
    [
        pytest.param("Grep", {"pattern": "def login", "path": "src/"}, "def login", "src/", id="grep-pattern-scoped"),
        pytest.param("Grep", {"pattern": "def login"}, "def login", ".", id="grep-pattern-unscoped"),
        pytest.param("Glob", {"pattern": "**/*.py"}, "**/*.py", ".", id="glob-pattern"),
        pytest.param("Glob", {"pattern": "*.py", "path": "src/pkg"}, "*.py", "src/pkg", id="glob-pattern-scoped"),
        pytest.param("Read", {"file_path": "/repo/src/auth.py"}, "/repo/src/auth.py", None, id="read-file-path"),
    ],
)
def test_appends_record_per_tool(
    tool: str, tool_input: dict, expected_target: str, expected_search_path: str | None, tmp_path: Path
) -> None:
    """Each Grep/Read/Glob call writes one line carrying its tool name, target, and any search scope.

    ``target`` is the pattern when one is set, so a scoped Grep/Glob also records ``search_path``; the overlap join
    reads it to tell a search of one file from a tree walk. Omitted search paths use the current directory; Reads carry
    no ``search_path`` key.
    """
    result = _run({"tool_name": tool, "tool_input": tool_input}, tmp_path)

    assert result.returncode == 0, result.stderr
    records = _read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["tool"] == tool
    assert records[0]["target"] == expected_target
    assert records[0].get("search_path") == (str(tmp_path) if expected_search_path == "." else expected_search_path)
    assert records[0]["layer"] == "tool"


def test_session_shard_uses_seeded_session_id(tmp_path: Path) -> None:
    """A seeded session tmpfile routes records to tools_<session>.jsonl, matching the cli shard key."""
    # seed-session.js writes codemap-<project>-session into TMPDIR; project = cwd basename here.
    session = "abc-123"
    (tmp_path / f"codemap-{tmp_path.name}-session").write_text(session)
    env = {**os.environ, "TMPDIR": str(tmp_path), "TEMP": str(tmp_path), "TMP": str(tmp_path)}
    env.pop("CODEMAP_LOG_DIR", None)
    env["CODEMAP_LOGGING"] = "true"  # conftest autouse gate exports false suite-wide
    result = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "x"}}),
        text=True,
        capture_output=True,
        cwd=str(tmp_path),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    shard = tmp_path / ".cache" / "codemap" / "logs" / "claude" / f"tools_{session}.jsonl"
    assert shard.exists(), "record not routed to the seeded per-session shard"
    assert json.loads(shard.read_text().strip())["session"] == session


@pytest.mark.integration
@pytest.mark.parametrize("scope", ["file", "directory", "legacy"])
def test_search_scope_survives_hook_to_join(tmp_path: Path, scope: str) -> None:
    """A real hook record distinguishes file inspection, directory search and unknowable old scope."""
    source = tmp_path / "pkg" / "mod.py"
    source.parent.mkdir()
    source.write_text("import pkg.mod\n")
    path = source if scope != "directory" else source.parent
    result = _run(
        {"tool_name": "Grep", "session_id": "scope-session", "tool_input": {"pattern": "pkg.mod", "path": str(path)}},
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    (record,) = _read_records(tmp_path)
    if scope == "legacy":
        record.pop("search_path", None)
        record.pop("search_scope", None)
    else:
        assert record["search_path"] == str(path)
        assert record["search_scope"] == scope
    answer = record | {
        "layer": "cli",
        "cmd": "rdeps",
        "exit_code": 0,
        "result": {"module": "pkg.mod", "index": {"query_complete": True}},
    }
    summary = ja.summarize(ja.parse_cli_records([answer]), ja.parse_tool_records([record]))
    payload = json.loads(ja.render_json(summary))
    assert payload["avoidance_count"] == 1
    assert (
        payload["events"][0]["kind"]
        == {"file": "source_read", "directory": "structural_search", "legacy": "unknown"}[scope]
    )
    assert payload["structural_search_count"] == int(scope == "directory")


@pytest.mark.parametrize(
    "payload",
    [
        # A tool outside the matched set (defence-in-depth vs the matcher).
        pytest.param({"tool_name": "Edit", "tool_input": {"file_path": "/x.py"}}, id="non-search-tool"),
        # Bash commands that are not manual search volume.
        pytest.param({"tool_name": "Bash", "tool_input": {"command": "ls -la src/"}}, id="bash-plain-listing"),
        pytest.param(
            {"tool_name": "Bash", "tool_input": {"command": "scan-query rdeps pkg.mod | grep imported_by"}},
            id="bash-scan-query-pipe",
        ),
    ],
)
def test_non_search_call_ignored(tmp_path: Path, payload: dict) -> None:
    """A tool outside the matched set or a Bash command that is not manual search volume writes nothing."""
    _run(payload, tmp_path)
    assert _read_records(tmp_path) == []


@pytest.mark.parametrize("command", ["rg 'def login' src/", "cat f.py | grep import"])
def test_bash_search_logged_with_command_target(tmp_path: Path, command: str) -> None:
    """Search-shaped Bash commands are logged as tool=Bash with the command as target."""
    _run({"tool_name": "Bash", "tool_input": {"command": command}}, tmp_path)
    records = _read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["tool"] == "Bash"
    assert records[0]["target"] == command


@pytest.fixture(name="search_tree")
def _search_tree(tmp_path: Path) -> Path:
    """Create ``a.py`` and ``src/mod.py`` under *tmp_path* so Bash operands have something to stat."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("import pkg.mod\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    return tmp_path


def _bash_record(command: str, cwd: Path, payload_cwd: Path | None = None) -> dict:
    """Run one Bash search through the hook and return its single record."""
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(payload_cwd or cwd)}
    result = _run(payload, cwd)
    assert result.returncode == 0, result.stderr
    (record,) = _read_records(cwd)
    return record


class TestBashSearchScope:
    """Sessions without a Grep tool search through Bash, so the producer observes Bash search scope too.

    Only a standalone grep/rg command with established operands gets a file or directory scope; pipelines, lists,
    expansions and unknown options stay ``unknown`` rather than guessed.
    """

    @pytest.mark.parametrize(
        ("command", "search_path", "scope"),
        [
            pytest.param("rg -n foo src/", "src/", "directory", id="rg-directory"),
            pytest.param("grep -n x a.py", "a.py", "file", id="grep-file"),
            pytest.param("grep -rn x src", "src", "directory", id="grep-recursive-directory"),
            pytest.param("rg -n foo", "<cwd>", "directory", id="rg-defaults-to-cwd"),
            pytest.param("rg -n foo src/ 2>/dev/null", "src/", "directory", id="discarded-stderr"),
            pytest.param("rg -n 'a|b' a.py src", f"a.py{os.pathsep}src", "directory", id="quoted-pipe-two-operands"),
            pytest.param("grep -n x a.py src/mod.py", f"a.py{os.pathsep}src/mod.py", "file", id="two-files"),
            pytest.param("grep -n x src", "src", "unknown", id="grep-skips-directory-without-recursion"),
            pytest.param("rg -n foo missing/", "missing/", "unknown", id="missing-operand"),
            pytest.param("grep -rn '(' src", "src", "directory", id="quoted-operator-only-pattern"),
            pytest.param("rg -n 'a;b' src", "src", "directory", id="quoted-semicolon-pattern"),
            pytest.param(r"grep -n a\|b a.py", "a.py", "file", id="escaped-pipe-pattern"),
            pytest.param("grep -hn x a.py", "a.py", "file", id="grep-h-is-no-filename"),
        ],
    )
    def test_standalone_search_records_observed_scope(
        self, search_tree: Path, command: str, search_path: str, scope: str
    ) -> None:
        """Operands are stat-ed at hook time: files are ``file``, a directory the tool descends into is
        ``directory``."""
        record = _bash_record(command, search_tree)

        assert record["search_path"] == (str(search_tree) if search_path == "<cwd>" else search_path)
        assert record["search_scope"] == scope

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("rg -n foo src | head", id="pipeline"),
            pytest.param("git show HEAD:a.py | grep foo", id="search-reads-stdin"),
            pytest.param("cd src && rg foo", id="command-list"),
            pytest.param("grep foo", id="grep-reads-stdin"),
            pytest.param("rg foo $DIR", id="variable-operand"),
            pytest.param("rg foo src/*.py", id="glob-operand"),
            pytest.param("rg --not-a-real-flag foo src", id="unknown-long-option"),
            pytest.param("rg foo 'src", id="unbalanced-quote"),
            pytest.param("rg foo src > out.txt", id="output-redirection"),
            pytest.param("rg foo src\nrg bar a.py", id="two-lines"),
            pytest.param("rg -h foo src", id="rg-short-help"),
            pytest.param("rg -V", id="rg-version"),
            pytest.param("grep -V", id="grep-version"),
        ],
    )
    def test_ambiguous_command_stays_unknown(self, search_tree: Path, command: str) -> None:
        """Anything that is not one simple search with literal operands records ``unknown`` and no path."""
        record = _bash_record(command, search_tree)

        assert record["search_scope"] == "unknown"
        assert "search_path" not in record

    def test_relative_operand_resolves_against_event_cwd(self, search_tree: Path) -> None:
        """The host event's ``cwd`` (where the Bash tool ran) anchors relative operands, not the hook's own cwd."""
        record = _bash_record("grep -n x mod.py", search_tree, payload_cwd=search_tree / "src")

        assert record["search_scope"] == "file"

    def test_quoted_native_absolute_path_is_observed(self, search_tree: Path) -> None:
        """A double-quoted host-native absolute path, backslashes included on Windows, stays one stat-able operand."""
        target = search_tree / "src" / "mod.py"

        record = _bash_record(f'grep -n x "{target}"', search_tree)

        assert record["search_path"] == str(target)
        assert record["search_scope"] == "file"

    @pytest.mark.parametrize(
        ("command", "operands"),
        [
            pytest.param(r'grep -n x "C:\proj\a.py"', ("C:\\proj\\a.py",), id="double-quoted-windows-path"),
            pytest.param(r"rg foo 'C:\proj\src'", ("C:\\proj\\src",), id="single-quoted-windows-path"),
            pytest.param("rg foo C:/proj/src", ("C:/proj/src",), id="forward-slash-windows-path"),
            pytest.param(r'"C:\tools\rg.exe" foo src', ("src",), id="windows-executable-path"),
            pytest.param(r"rg foo C:\proj\src", ("C:projsrc",), id="unquoted-backslashes-follow-bash"),
        ],
    )
    def test_windows_shaped_operands_follow_posix_shell_quoting(self, command: str, operands: tuple[str, ...]) -> None:
        """The Bash tool is a POSIX shell on every host (Git Bash on Windows), so quoting decides what reaches grep/rg.

        A quoted Windows path keeps its backslashes; an unquoted one loses them exactly as bash would remove them.
        """
        assert _MODULE.bash_search_operands(command).operands == operands


@pytest.mark.integration
@pytest.mark.parametrize(
    ("command", "kind"),
    [
        pytest.param("rg -n pkg.mod pkg", "structural_search", id="directory-search"),
        pytest.param("grep -n pkg.mod pkg/mod.py", "source_read", id="single-file-search"),
        pytest.param("rg -n pkg.mod pkg | head", "unknown", id="pipeline-stays-unknown"),
    ],
)
def test_bash_search_scope_survives_hook_to_join(tmp_path: Path, command: str, kind: str) -> None:
    """A real Bash hook record is classified by its observed scope, so tree searches finally reach the structural count.

    The pipeline case keeps the target-spelling fallback: ``rg`` recurses, so its unverified scope stays ``unknown``.
    """
    source = tmp_path / "pkg" / "mod.py"
    source.parent.mkdir()
    source.write_text("import pkg.mod\n")
    payload = {
        "tool_name": "Bash",
        "session_id": "bash-scope",
        "tool_input": {"command": command},
        "cwd": str(tmp_path),
    }
    assert _run(payload, tmp_path).returncode == 0
    (record,) = _read_records(tmp_path)
    answer = record | {
        "layer": "cli",
        "cmd": "rdeps",
        "exit_code": 0,
        "result": {"module": "pkg.mod", "index": {"query_complete": True}},
    }

    summary = json.loads(ja.render_json(ja.summarize(ja.parse_cli_records([answer]), ja.parse_tool_records([record]))))

    assert summary["events"][0]["kind"] == kind
    assert summary["structural_search_count"] == int(kind == "structural_search")


def test_records_carry_plugin_version(tmp_path: Path) -> None:
    """Every record stamps the plugin version `v` for before/after release comparison."""
    _run({"tool_name": "Read", "tool_input": {"file_path": "/a/b.py"}}, tmp_path)
    (record,) = _read_records(tmp_path)
    assert record["v"] not in ("", "?")


def test_tool_response_never_parsed(tmp_path: Path) -> None:
    """A non-JSON, oversized tool_response must not affect the append — the hook never reads it."""
    payload = {
        "tool_name": "Grep",
        "tool_input": {"pattern": "def login"},
        "tool_response": "\x00not-json{{{" + "x" * 100000,
    }
    result = _run(payload, tmp_path)

    assert result.returncode == 0, result.stderr
    records = _read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["target"] == "def login"


def test_logging_disabled_suppresses_record(tmp_path: Path) -> None:
    """Suppress telemetry shards when logging is disabled."""
    result = _run({"tool_name": "Grep", "tool_input": {"pattern": "x"}}, tmp_path, logging="false")

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".cache" / "codemap" / "logs").exists()


class TestReadRedundancyNudge:
    """3rd Read of the same non-test .py file prints one structural-query hint."""

    _PAYLOAD: ClassVar = {"tool_name": "Read", "tool_input": {"file_path": "/proj/src/core.py"}}

    def test_hint_fires_exactly_on_third_read(self, tmp_path: Path) -> None:
        """Reads 1–2 stay silent, read 3 hints, read 4 stays silent again."""
        outs = [_run(self._PAYLOAD, tmp_path).stdout for _ in range(4)]
        assert outs[0] == ""
        assert outs[1] == ""
        assert "[codemap] core.py read 3x" in outs[2]
        assert outs[3] == ""

    def test_no_hint_for_test_files(self, tmp_path: Path) -> None:
        """Repeated Reads of test files never nudge — re-reading tests is normal."""
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/proj/tests/test_core.py"}}
        outs = [_run(payload, tmp_path).stdout for _ in range(4)]
        assert all(o == "" for o in outs)

    def test_hint_survives_a_large_preexisting_shard(self, tmp_path: Path) -> None:
        """The nudge still fires on the 3rd read when a big shard already exists.

        Counting used to mean decoding and splitting the whole shard — up to the 10 MB rotation budget — on every
        matched Read, to decide one advisory.
        """
        log_dir = tmp_path / ".cache" / "codemap" / "logs" / "claude"
        log_dir.mkdir(parents=True)
        filler = json.dumps({"ts": "2026-01-01T00:00:00Z", "tool": "Grep", "target": "x" * 200}) + "\n"
        (log_dir / "tools.jsonl").write_text(filler * 5_000)

        outs = [_run(self._PAYLOAD, tmp_path).stdout for _ in range(3)]

        assert outs[0] == ""
        assert outs[1] == ""
        assert "[codemap] core.py read 3x" in outs[2]


class TestTailLines:
    """The bounded window the repeated-read nudge counts over."""

    def test_short_file_is_returned_whole(self, tmp_path: Path) -> None:
        """A file inside the window yields every line, so small shards count exactly."""
        log_file = tmp_path / "tools.jsonl"
        log_file.write_text("one\ntwo\nthree\n")

        assert _MODULE.tail_lines(log_file, 1024) == ["one", "two", "three"]

    def test_window_is_bounded_and_drops_the_partial_head(self, tmp_path: Path) -> None:
        """Only the trailing window is read, and its truncated first line is discarded.

        A half-line at the window's head can still contain the searched-for target and would inflate the count, so it
        never reaches the caller.
        """
        log_file = tmp_path / "tools.jsonl"
        log_file.write_text("".join(f"line-{index:04d}\n" for index in range(1000)))

        lines = _MODULE.tail_lines(log_file, 100)

        assert len(lines) < 20, "the whole file was read despite the window"
        assert lines[-1] == "line-0999"
        assert all(line.startswith("line-") and len(line) == 9 for line in lines)
