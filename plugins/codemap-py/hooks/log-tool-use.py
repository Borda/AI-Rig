#!/usr/bin/env python3
"""Record search and read telemetry for codemap guidance.

Purpose:
    Track low-cost tool-use signals and print one advisory when a source file is read
    repeatedly, helping callers choose structural codemap queries.

Scope:
    Parse one host tool event, append one compact JSONL record, and inspect a bounded
    tail for the repeated-read threshold. It does not run searches or alter source files.
    Matched events must supply mapping-shaped ``tool_input`` when that field is truthy.

Usage:
    Invoke as a Claude or Codex tool-use hook with the host event JSON on standard input.

Outputs:
    Append one runtime-scoped JSONL record and, at most once per qualifying read, print
    a codemap query hint.

Failure:
    JSON decoding, type/value, and filesystem errors are ignored. Other errors propagate;
    a truthy non-mapping ``tool_input`` raises ``AttributeError`` rather than failing open.

Used by:
    Codemap-py tool-use hook configuration and the runtime telemetry joiner.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import NamedTuple

#: Claude launches this hook as `python "<plugin-root>/hooks/log-tool-use.py"`, which
#: already puts hooks/ on sys.path — but the test suite loads it through
#: `importlib.util.spec_from_file_location`, which does not. Inserting explicitly makes
#: the shared-helper import resolve under every load mechanism.
_HOOKS_DIR = Path(__file__).resolve().parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import _hookutil  # noqa: E402  (needs the sys.path insert above)

#: Size in bytes above which a session's tool-use shard is rotated before the next record is appended.
_LOG_MAX_BYTES = 10 * 1024 * 1024
#: Matches a Bash command that starts a ``grep``-family or ``rg`` search, also after a pipe, ``;``, ``&`` or ``(``.
_BASH_SEARCH = re.compile(r"(^|[|;&(]\s*)(rg|grep|egrep|fgrep)\s")
#: Bytes of the shard the repeated-read nudge inspects. It runs on every matched Read, so
#: scanning the whole 10 MB budget to decide one advisory was the dominant cost of a hook
#: whose entire contract is to stay cheap. ~1.7K records fit here, far more than the three
#: the nudge counts; beyond that window the hint can fire one read late, never spuriously.
_NUDGE_TAIL_BYTES = 256 * 1024
#: Same sanitizer as ``codemap_py.telemetry`` — the shard names must agree to join.
_UNSAFE_KEY = re.compile(r"[^A-Za-z0-9_-]")
#: Stderr/stdout discards and fd duplications (``2>/dev/null``, ``2>&1``): they change no search operand, and agents
#: append them to most searches. Every other redirection still makes the scope unknown.
_DISCARDED_REDIRECT = re.compile(r"\s(?:[12]?>>?|&>)\s*/dev/null(?=\s|$)|\s[12]?>&[12](?=\s|$)")
#: Characters shlex reports as shell operators; a token made only of them is a pipe, list, subshell or redirection.
_OPERATOR_CHARS = frozenset("();<>|&")
#: Operand characters the shell would expand (variables, substitution, globs, braces, home), so the word is not a path.
_EXPANDING_CHARS = frozenset("$`*?[{")
#: grep-family names sharing one option grammar.
_GREP_NAMES = frozenset({"grep", "egrep", "fgrep"})
#: Short options that take a value (rest of the cluster or the next word), from the GNU/BSD grep and ripgrep manuals.
#: ripgrep's ``-r`` is ``--replace``, not recursion, and ``-d`` is ``--max-depth``.
_SHORT_WITH_VALUE = {"grep": frozenset("efmABCdD"), "rg": frozenset("efgtTmABCjMrEd")}
#: Long options that take a value as ``--opt=value`` or ``--opt value``.
_LONG_WITH_VALUE = {
    "grep": frozenset(
        "regexp file max-count after-context before-context context directories devices exclude exclude-from "
        "exclude-dir include label binary-files group-separator".split()
    ),
    "rg": frozenset(
        "regexp file glob iglob type type-not type-add type-clear max-count after-context before-context context "
        "threads max-columns max-depth replace encoding pre pre-glob sort sortr color colors context-separator "
        "field-match-separator field-context-separator path-separator dfa-size-limit regex-size-limit max-filesize "
        "engine ignore-file hostname-bin hyperlink-format".split()
    ),
}
#: Long options known to take no separate value. Any other long option makes the operands ambiguous, so the scope is
#: reported unknown instead of risking a value read as a path.
_LONG_FLAGS = {
    "grep": frozenset(
        "extended-regexp fixed-strings basic-regexp perl-regexp ignore-case no-ignore-case word-regexp line-regexp "
        "null-data no-messages invert-match byte-offset line-number line-buffered with-filename no-filename "
        "only-matching quiet silent text recursive dereference-recursive files-without-match files-with-matches "
        "count initial-tab null color colour no-group-separator".split()
    ),
    "rg": frozenset(
        "line-number no-line-number column no-column heading no-heading hidden no-hidden no-ignore no-ignore-vcs "
        "no-ignore-dot no-ignore-parent no-ignore-global no-ignore-exclude no-ignore-files files files-with-matches "
        "files-without-match count count-matches fixed-strings ignore-case smart-case case-sensitive word-regexp "
        "line-regexp invert-match json vimgrep multiline multiline-dotall pcre2 follow no-follow only-matching "
        "no-filename with-filename stats unrestricted text binary search-zip null null-data trim passthru "
        "sort-files quiet crlf byte-offset block-buffered line-buffered no-messages no-config no-unicode pretty "
        "max-columns-preview include-zero one-file-system no-require-git glob-case-insensitive no-pre".split()
    ),
}
#: Long options that print help or metadata instead of searching.
_NO_SEARCH_LONG = frozenset({"help", "version", "type-list", "generate"})
#: Short options that print help or a version instead of searching; grep's ``-h`` is ``--no-filename``, so only
#: ripgrep's ``-h`` belongs here.
_NO_SEARCH_SHORT = {"grep": frozenset("V"), "rg": frozenset("hV")}


class BashSearch(NamedTuple):
    """Path operands of one standalone grep/rg command and whether it descends into directory operands."""

    operands: tuple[str, ...]
    walks_directories: bool


def iso_now() -> str:
    """Return the compact UTC timestamp format used by codemap telemetry."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# Re-exported from the shared helper so this hook and ``seed-session.py`` — the marker's
# reader and its writer — cannot key on different names.
project_name = _hookutil.project_name


def session_id(payload: dict | None = None) -> str:
    """Prefer the current host event; use Claude's marker only for legacy events."""
    session = _hookutil.runtime_session(payload, telemetry=True)
    if session or _hookutil.runtime() == "codex":
        return session
    marker = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / f"codemap-{project_name()}-session"
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def rotate(log_file: Path) -> None:
    """Rotate the bounded telemetry file, retaining two prior generations."""
    for generation in (2, 1):
        source = Path(f"{log_file}.{generation}")
        if source.exists():
            source.replace(Path(f"{log_file}.{generation + 1}"))
    if log_file.exists():
        log_file.replace(Path(f"{log_file}.1"))


def target_for(tool_name: str, tool_input: dict) -> str:
    """Return the one telemetry target field appropriate for the host tool."""
    if tool_name == "Read":
        return str(tool_input.get("file_path", ""))
    if tool_name == "Bash":
        return str(tool_input.get("command", ""))[:200]
    return str(tool_input.get("pattern") or tool_input.get("path") or "")


def _has_unquoted_operator(command: str) -> bool:
    """Return whether *command* has a shell operator character outside quotes and not backslash-escaped.

    Tracked here rather than read back from ``shlex`` tokens, which lose quoting: ``grep -rn '(' pkg`` searches for a
    literal parenthesis, while ``grep -rn ( pkg`` is a subshell.

    Examples:
        >>> _has_unquoted_operator("grep -rn '(' pkg"), _has_unquoted_operator(r"grep -n a\\|b f.py")
        (False, False)
        >>> _has_unquoted_operator('rg "x" src | head'), _has_unquoted_operator("cd src && rg x")
        (True, True)
    """
    quote = ""
    escaped = False
    for char in command:
        if escaped:
            escaped = False
        elif quote == "'":
            quote = "" if char == "'" else quote
        elif char == "\\":
            escaped = True
        elif quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char in _OPERATOR_CHARS:
            return True
    return False


def _shell_words(command: str) -> list[str] | None:
    """Split one shell command into words the way bash would, or return ``None`` when it is not one simple command.

    POSIX quoting rules apply because Claude's Bash tool runs a POSIX shell on every platform (Git Bash on Windows): an
    unquoted backslash escapes, a quoted Windows path keeps its backslashes. A native PowerShell command line (Codex on
    Windows) quotes differently and is not modelled; its operands usually fail to resolve and the scope stays unknown.
    Pipelines, lists, subshells, newlines and redirections other than the discards in :data:`_DISCARDED_REDIRECT` are
    refused, never guessed through.
    """
    command = _DISCARDED_REDIRECT.sub(" ", command)
    if "\n" in command or _has_unquoted_operator(command):
        return None
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return None
    return words or None


def _search_tool(word: str) -> str | None:
    """Return ``"grep"`` or ``"rg"`` for a command word naming one, path-qualified or ``.exe`` included."""
    name = PureWindowsPath(word).name.lower().removesuffix(".exe")
    if name in _GREP_NAMES:
        return "grep"
    return "rg" if name == "rg" else None


class _ArgScan:
    """Running state while one grep/rg argument list is split into options and positional words.

    A plain class, not a dataclass: the hook is also loaded through ``spec_from_file_location`` without a
    ``sys.modules`` entry, where ``@dataclass`` cannot resolve its module and fails at import.
    """

    __slots__ = ("files_mode", "pattern_from_option", "positional", "recursive", "tool")

    def __init__(self, tool: str) -> None:
        """Start an empty scan for one ``grep`` or ``rg`` argument list."""
        self.tool = tool
        self.positional: list[str] = []
        self.pattern_from_option = False
        self.recursive = False
        self.files_mode = False

    def take_long(self, arg: str, rest: Iterator[str]) -> bool:
        """Consume one ``--option[=value]``; return False when its arity is unknown or it does not search."""
        name, has_value, value = arg[2:].partition("=")
        takes_value = name in _LONG_WITH_VALUE[self.tool]
        if name in _NO_SEARCH_LONG or not (takes_value or name in _LONG_FLAGS[self.tool]):
            return False
        if takes_value and not has_value:
            value = next(rest, None)
            if value is None:
                return False
        self.pattern_from_option |= name in {"regexp", "file"}
        self.recursive |= name in {"recursive", "dereference-recursive"} or (name, value) == ("directories", "recurse")
        self.files_mode |= name == "files"
        return True

    def take_short(self, arg: str, rest: Iterator[str]) -> bool:
        """Consume one short-option cluster such as ``-rnA3``; return False on a missing value or a no-search flag."""
        cluster = arg[1:]
        for index, letter in enumerate(cluster):
            if letter in _NO_SEARCH_SHORT[self.tool]:
                return False
            if letter in _SHORT_WITH_VALUE[self.tool]:
                value = cluster[index + 1 :] or next(rest, None)
                if value is None:
                    return False
                self.pattern_from_option |= letter in "ef"
                self.recursive |= self.tool == "grep" and letter == "d" and value == "recurse"
                return True
            self.recursive |= self.tool == "grep" and letter in "rR"
        return True


def _parse_search_args(tool: str, args: list[str]) -> BashSearch | None:
    """Separate path operands from options for one grep or ripgrep invocation; ``None`` when ambiguous.

    Options may follow operands (both tools permute), ``--`` ends option parsing, and ``-e``/``-f`` (or
    ``--regexp``/``--file``) supply the pattern so every positional word is a path. An option whose arity is not known
    makes the result ambiguous rather than risk reading its value as a path.

    Examples:
        >>> _parse_search_args("rg", ["-n", "foo", "src/"])
        BashSearch(operands=('src/',), walks_directories=True)
        >>> _parse_search_args("grep", ["-rn", "-e", "x", "--", "-odd.py", "src"])
        BashSearch(operands=('-odd.py', 'src'), walks_directories=True)
        >>> _parse_search_args("grep", ["-n", "x", "a.py", "--weird"]) is None
        True
    """
    scan = _ArgScan(tool)
    rest = iter(args)
    for arg in rest:
        if arg == "--":
            scan.positional.extend(rest)
            break
        if arg.startswith("--"):
            understood = scan.take_long(arg, rest)
        elif arg.startswith("-") and arg != "-":
            understood = scan.take_short(arg, rest)
        else:
            scan.positional.append(arg)
            understood = True
        if not understood:
            return None
    operands = scan.positional
    if not (scan.pattern_from_option or scan.files_mode):
        if not operands:
            return None
        operands = operands[1:]
    return BashSearch(tuple(operands), walks_directories=tool == "rg" or scan.recursive)


def bash_search_operands(command: str) -> BashSearch | None:
    """Return the path operands of a standalone grep/rg command, or ``None`` when its scope cannot be established.

    An empty operand tuple means ripgrep's default: search the working directory. grep without operands reads standard
    input (or, depending on platform, the working directory under ``-r``), so it is ambiguous. Words the shell would
    expand (``$VAR``, globs, braces, ``~``) or ``-`` (standard input) are not paths and make the scope ambiguous too.

    Examples:
        >>> bash_search_operands("rg -n foo src/")
        BashSearch(operands=('src/',), walks_directories=True)
        >>> bash_search_operands('grep -n x "C:\\\\proj\\\\a.py" 2>/dev/null')
        BashSearch(operands=('C:\\\\proj\\\\a.py',), walks_directories=False)
        >>> bash_search_operands("rg -n foo src | head") is None
        True
        >>> bash_search_operands("git show HEAD:a.py | grep foo") is None
        True
    """
    words = _shell_words(command)
    tool = _search_tool(words[0]) if words else None
    if tool is None:
        return None
    search = _parse_search_args(tool, words[1:])
    if search is None or (not search.operands and tool == "grep"):
        return None
    if any(op == "-" or op.startswith("~") or _EXPANDING_CHARS & set(op) for op in search.operands):
        return None
    return search


def path_scope(path: Path) -> str:
    """Return ``file``, ``directory`` or ``unknown`` for *path* as it exists at hook time."""
    try:
        if path.is_file():
            return "file"
        if path.is_dir():
            return "directory"
    except OSError:
        pass
    return "unknown"


def bash_search_scope(command: str, cwd: Path) -> tuple[str | None, str]:
    """Observe the search path and scope of one Bash search command while the producer can still stat it.

    A command searching only files is ``file``; one whose tool descends into at least one directory operand is
    ``directory``. A missing operand, a directory grep would skip without ``-r``, or any parse ambiguity is
    ``unknown``.

    Args:
        command: the full Bash command (never the 200-character logged target).
        cwd: directory relative operands resolve against.

    Returns:
        ``(search_path, search_scope)``; ``search_path`` is ``None`` when no operand could be established, the
        working directory for ripgrep without operands, and :data:`os.pathsep`-joined for several operands.
    """
    search = bash_search_operands(command)
    if search is None:
        return None, "unknown"
    if not search.operands:
        return str(cwd), path_scope(cwd)
    scopes = {path_scope(cwd / operand) for operand in search.operands}
    if scopes == {"file"}:
        scope = "file"
    elif scopes <= {"file", "directory"} and search.walks_directories:
        scope = "directory"
    else:
        scope = "unknown"
    return os.pathsep.join(search.operands), scope


def tail_lines(log_file: Path, limit: int) -> list[str]:
    """Return the last *limit* bytes of *log_file* as whole lines.

    A window that starts mid-record would otherwise hand the caller a truncated first line
    that can still contain a searched-for substring, so it is dropped whenever the read did
    not start at byte 0.

    Args:
        log_file: The telemetry shard to read.
        limit: Maximum number of trailing bytes to inspect.

    Returns:
        Complete lines from the window, oldest first.
    """
    with log_file.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        start = max(0, size - limit)
        stream.seek(start)
        window = stream.read()
    lines = window.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start and lines else lines


def maybe_nudge_repeated_read(record: dict, log_file: Path) -> None:
    """Print one hint when a non-test Python source file is read for the third time."""
    if record["tool"] != "Read" or not record["target"].endswith(".py"):
        return
    if re.search(r"/tests?/", record["target"]):
        return
    escaped_target = json.dumps(record["target"])
    try:
        count = sum('"Read"' in line and escaped_target in line for line in tail_lines(log_file, _NUDGE_TAIL_BYTES))
    except OSError:
        return
    if count == 3:
        base = Path(record["target"]).stem
        print(
            f"[codemap] {base}.py read 3x this session — structural queries may be cheaper: "
            "codemap-py query symbol --with-imports <name>, rdeps <module>, fn-rdeps <module::fn>"
        )


def main() -> int:
    """Record one matched tool use, respecting the opt-out and fail-open contracts."""
    if os.environ.get("CODEMAP_LOGGING", "true").lower() == "false":
        return 0
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        tool_name = str(payload.get("tool_name", ""))
        if tool_name not in {"Grep", "Read", "Glob", "Bash"}:
            return 0
        tool_input = payload.get("tool_input") or {}
        if tool_name == "Bash":
            command = str(tool_input.get("command", ""))
            if not _BASH_SEARCH.search(command) or "scan-query" in command:
                return 0
        session = session_id(payload)
        safe_session = _UNSAFE_KEY.sub("-", session)
        record = {
            "ts": iso_now(),
            "layer": "tool",
            "runtime": _hookutil.runtime(),
            "v": _hookutil.plugin_version(),
            "tool": tool_name,
            "session": session,
            "project": _hookutil.project_root().as_posix(),
            "target": target_for(tool_name, tool_input),
        }
        if tool_name in {"Grep", "Glob"}:
            # Capture scope while the producer can inspect it; the analysis host must not stat old paths.
            search_path = str(tool_input.get("path") or Path.cwd())
            record["search_path"] = search_path
            record["search_scope"] = path_scope(Path(search_path))
        elif tool_name == "Bash":
            # Sessions without a Grep tool search through Bash; parse the full command, not the truncated target.
            cwd = payload.get("cwd")
            search_path, record["search_scope"] = bash_search_scope(
                str(tool_input.get("command", "")), Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()
            )
            if search_path is not None:
                record["search_path"] = search_path
        log_dir = _hookutil.log_dir()
        log_file = log_dir / (f"tools_{safe_session}.jsonl" if safe_session else "tools.jsonl")
        log_dir.mkdir(parents=True, exist_ok=True)
        if log_file.exists() and log_file.stat().st_size > _LOG_MAX_BYTES:
            rotate(log_file)
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        maybe_nudge_repeated_read(record, log_file)
    except (OSError, TypeError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
