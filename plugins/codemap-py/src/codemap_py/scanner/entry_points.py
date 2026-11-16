"""Entry-point references declared in the project's root ``pyproject.toml``.

Packaging metadata names callables that run with no static importer: console and GUI scripts (``[project.scripts]``,
``[project.gui-scripts]``) and plugin groups (``[project.entry-points.<group>]``, including pytest's ``pytest11``). The
dead-code queries treat each declared target as referenced.

The parser is table-scoped and line-based rather than a full TOML parser: ``tomllib`` is Python 3.11+ while the project
supports 3.10, and the index must not depend on the interpreter that built it. Only the entry-point tables (and the
matching keys of the ``[project]`` table) are parsed, and strictly; every other line is skipped, apart from tracking
multi-line strings and arrays so a header-looking line inside a ``\"\"\"`` string or a multi-line array never opens a
table. When any in-scope line does not parse, or the file ends inside a multi-line string or array, the whole file
contributes no entry points, so malformed metadata never yields a partial result. The root file is read as
``utf-8-sig``, so a leading byte order mark does not hide the first header. A well-formed value that is not a valid
object reference (``module`` or ``module:attr.path`` with optional ``[extras]``) is skipped on its own.

Out of scope: ``[tool.poetry.scripts]``, ``setup.cfg`` and ``setup.py`` entry points, and ``pyproject.toml`` files below
the project root.
"""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from pathlib import Path

from . import discovery

#: ``[project]`` sub-tables holding scripts, mapped to the entry-point group packaging tools register them under.
_SCRIPT_GROUPS: dict[str, str] = {"scripts": "console_scripts", "gui-scripts": "gui_scripts"}
#: ``[project]`` sub-table holding arbitrary entry-point groups, one nested table per group.
_ENTRY_POINTS_KEY = "entry-points"
#: Root table of the PEP 621 metadata.
_PROJECT = "project"
#: First key segments below ``[project]`` that the parser reads.
_SCOPE_KEYS = frozenset({*_SCRIPT_GROUPS, _ENTRY_POINTS_KEY})
#: Pseudo table path for an array-of-tables header; never in scope.
_ARRAY_TABLE: tuple[str, ...] = ("[[array-of-tables]]",)
#: Nesting bound for inline tables, so a hostile line cannot exhaust the recursion limit.
_MAX_INLINE_DEPTH = 8

_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_BASIC_STRING_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
_LITERAL_STRING_RE = re.compile(r"'([^'\n]*)'")
_ESCAPE_RE = re.compile(r'\\(?:([btnfr"\\])|u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8}))')
_SIMPLE_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}
_EXTRA_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_TARGET_RE = re.compile(
    r"\s*(?P<module>[^\s:\[\]]+)\s*(?::\s*(?P<attr>[^\s:\[\]]+))?\s*"
    rf"(?:\[\s*(?:{_EXTRA_NAME}\s*(?:,\s*{_EXTRA_NAME}\s*)*)?\])?\s*"
)


class _MalformedError(ValueError):
    """In-scope TOML text that the table-scoped parser cannot read."""


@dataclass(frozen=True, slots=True)
class EntryPoint:
    """One entry point declared in ``pyproject.toml``.

    Attributes:
        group: entry-point group (``console_scripts``, ``gui_scripts``, or the ``[project.entry-points]`` group).
        name: the entry point's own name, such as the console command.
        module: dotted module the target imports.
        symbol: dotted attribute path inside the module (``main`` or ``Cls.method``), or ``None`` for a module-only
            target.
        line: 1-based line of the declaring key.
    """

    group: str
    name: str
    module: str
    symbol: str | None
    line: int


class _Cursor:
    """Position inside one line of TOML text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def skip_ws(self) -> None:
        """Advance past spaces and tabs."""
        while self.pos < len(self.text) and self.text[self.pos] in " \t":
            self.pos += 1

    def peek(self) -> str:
        """Return the next character after whitespace, or an empty string at the end."""
        self.skip_ws()
        return self.text[self.pos : self.pos + 1]

    def expect(self, char: str) -> None:
        """Consume *char* after whitespace or raise ``_MalformedError``."""
        if self.peek() != char:
            raise _MalformedError(f"expected {char!r}")
        self.pos += 1

    def at_line_end(self) -> bool:
        """Return whether only whitespace or a comment remains."""
        return self.peek() in ("", "#")


def _decode_basic_string(body: str) -> str:
    """Decode the escapes of a TOML basic string body, rejecting any escape TOML does not define."""
    if "\\" in _ESCAPE_RE.sub("", body):
        raise _MalformedError("invalid escape")

    def replace(match: re.Match[str]) -> str:
        if match.group(1):
            return _SIMPLE_ESCAPES[match.group(1)]
        code = int(match.group(2) or match.group(3), 16)
        if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            raise _MalformedError("invalid unicode escape")
        return chr(code)

    return _ESCAPE_RE.sub(replace, body)


def _read_string(cur: _Cursor) -> str:
    """Read one single-line basic or literal string at the cursor."""
    cur.skip_ws()
    basic = _BASIC_STRING_RE.match(cur.text, cur.pos)
    if basic:
        cur.pos = basic.end()
        return _decode_basic_string(basic.group(1))
    literal = _LITERAL_STRING_RE.match(cur.text, cur.pos)
    if literal:
        cur.pos = literal.end()
        return literal.group(1)
    raise _MalformedError("expected a string")


def _read_key_path(cur: _Cursor) -> tuple[str, ...]:
    """Read a dotted key whose segments are bare words or quoted strings."""
    segments: list[str] = []
    while True:
        if cur.peek() in ('"', "'"):
            segments.append(_read_string(cur))
        else:
            bare = _BARE_KEY_RE.match(cur.text, cur.pos)
            if not bare:
                raise _MalformedError("expected a key")
            cur.pos = bare.end()
            segments.append(bare.group())
        if cur.peek() != ".":
            return tuple(segments)
        cur.pos += 1


def _read_value(cur: _Cursor, depth: int = 0) -> list[tuple[tuple[str, ...], str]]:
    """Read a string or inline-table value as ``(relative key path, string)`` leaves."""
    if cur.peek() == "{":
        return _read_inline_table(cur, depth + 1)
    return [((), _read_string(cur))]


def _read_inline_table(cur: _Cursor, depth: int) -> list[tuple[tuple[str, ...], str]]:
    """Read a single-line inline table whose leaves are strings; a trailing comma is rejected as TOML 1.0 does."""
    if depth > _MAX_INLINE_DEPTH:
        raise _MalformedError("inline tables nested too deeply")
    cur.expect("{")
    leaves: list[tuple[tuple[str, ...], str]] = []
    if cur.peek() == "}":
        cur.pos += 1
        return leaves
    while True:
        path = _read_key_path(cur)
        cur.expect("=")
        leaves.extend((path + sub, value) for sub, value in _read_value(cur, depth))
        if cur.peek() != ",":
            cur.expect("}")
            return leaves
        cur.pos += 1


def _read_assignment(line: str) -> list[tuple[tuple[str, ...], str]]:
    """Parse one ``key = value`` line into ``(key path, string)`` leaves."""
    cur = _Cursor(line)
    path = _read_key_path(cur)
    cur.expect("=")
    leaves = [(path + sub, value) for sub, value in _read_value(cur)]
    if not cur.at_line_end():
        raise _MalformedError("unexpected text after value")
    return leaves


def _read_header(line: str) -> tuple[str, ...]:
    """Parse a ``[table]`` header line; an ``[[array]]`` header yields the out-of-scope ``_ARRAY_TABLE`` path."""
    cur = _Cursor(line)
    cur.expect("[")
    is_array = cur.text[cur.pos : cur.pos + 1] == "["
    if is_array:
        cur.pos += 1
    path = _read_key_path(cur)
    cur.expect("]")
    if is_array:
        if cur.text[cur.pos : cur.pos + 1] != "]":
            raise _MalformedError("unclosed array-of-tables header")
        cur.pos += 1
    if not cur.at_line_end():
        raise _MalformedError("unexpected text after header")
    return _ARRAY_TABLE if is_array else path


def _table_in_scope(table: tuple[str, ...]) -> bool:
    """Return whether every line of *table* belongs to the entry-point metadata."""
    return len(table) >= 2 and table[0] == _PROJECT and table[1] in _SCOPE_KEYS


def _line_in_scope(table: tuple[str, ...], line: str) -> bool:
    """Return whether *line* inside *table* declares entry-point metadata and must parse strictly."""
    if _table_in_scope(table):
        return True
    if table != (_PROJECT,):
        return False
    cur = _Cursor(line)
    try:
        path = _read_key_path(cur)
        cur.expect("=")
    except _MalformedError:
        return False
    return path[0] in _SCOPE_KEYS


def _next_table(line: str, table: tuple[str, ...]) -> tuple[str, ...]:
    """Return the table a ``[``-prefixed line opens, keeping *table* for an unreadable header outside the scope."""
    try:
        return _read_header(line)
    except _MalformedError:
        if _table_in_scope(table):
            raise
        return table


@dataclass(frozen=True, slots=True)
class _OpenValue:
    """What a skipped line leaves open for the lines after it: a multi-line string, unclosed arrays, or neither.

    Attributes:
        string: delimiter (``\"\"\"`` or ``'''``) of a multi-line string that is still open, or ``None``.
        depth: number of ``[`` array brackets opened and not yet closed.
    """

    string: str | None = None
    depth: int = 0

    @property
    def is_open(self) -> bool:
        """Return whether the next line continues a value instead of starting a statement or a table header."""
        return self.string is not None or self.depth > 0


#: State between statements: no multi-line string or array is open.
_NOTHING_OPEN = _OpenValue()


def _multiline_string_end(line: str, pos: int, delim: str) -> int | None:
    """Return the index just past the delimiter closing a multi-line string on *line*, or ``None`` if it stays open.

    In a basic (``\"\"\"``) string a backslash escapes the next character. Up to two quotes directly before the
    delimiter belong to the string, as TOML allows, so a run of up to five quotes ends at its last one.

    Examples:
        >>> q = '"' * 3
        >>> _multiline_string_end("a\\\\" + q + " b" + q + " ]", 0, q)
        10
        >>> _multiline_string_end("still open", 0, "'''") is None
        True
    """
    while pos < len(line):
        if delim == '"""' and line[pos] == "\\":
            pos += 2
        elif line.startswith(delim, pos):
            run = len(line) - pos - len(line[pos:].lstrip(delim[0]))
            return pos + min(run, 5)
        else:
            pos += 1
    return None


def _single_line_string_end(line: str, pos: int) -> int:
    """Return the index just past the single-line string starting at *pos*, or the line end when it never closes."""
    pattern = _BASIC_STRING_RE if line[pos] == '"' else _LITERAL_STRING_RE
    match = pattern.match(line, pos)
    return match.end() if match else len(line)


def _advance_open_value(line: str, before: _OpenValue) -> _OpenValue:
    """Return what stays open after a skipped line, given what was open before it.

    Brackets count only outside strings and comments, so ``"uvicorn[standard]"`` or ``# ]`` never opens or closes an
    array; a multi-line string that closes mid-line hands the rest of the line back to the bracket count.

    Examples:
        >>> _advance_open_value('deps = [  # "]"', _NOTHING_OPEN)
        _OpenValue(string=None, depth=1)
        >>> _advance_open_value("  'a[b]', '''x", _OpenValue(depth=1))
        _OpenValue(string="'''", depth=1)
        >>> _advance_open_value("y''' ]", _OpenValue(string="'''", depth=1)).is_open
        False
    """
    string, depth, pos = before.string, before.depth, 0
    while pos < len(line):
        if string is not None:
            end = _multiline_string_end(line, pos, string)
            if end is None:
                break
            string, pos = None, end
        elif line.startswith(('"""', "'''"), pos):
            string, pos = line[pos : pos + 3], pos + 3
        elif line[pos] in "\"'":
            pos = _single_line_string_end(line, pos)
        elif line[pos] == "#":
            break
        else:
            depth = max(depth + {"[": 1, "]": -1}.get(line[pos], 0), 0)
            pos += 1
    return _OpenValue(string, depth)


def _is_dotted_identifier(text: str) -> bool:
    """Return whether *text* is a dotted path of Python identifiers, none a keyword."""
    return all(part.isidentifier() and not keyword.iskeyword(part) for part in text.split("."))


def parse_entry_point_target(value: str) -> tuple[str, str | None] | None:
    """Split an entry-point object reference into its module and attribute path.

    Args:
        value: the declared target, ``module`` or ``module:attr.path``, optionally followed by ``[extras]``.

    Returns:
        ``(module, attribute path or None)``, or ``None`` when *value* is not a valid object reference.

    Examples:
        >>> parse_entry_point_target("pkg.cli:main [color]")
        ('pkg.cli', 'main')
        >>> parse_entry_point_target("pkg.tools:Runner.run")
        ('pkg.tools', 'Runner.run')
        >>> parse_entry_point_target("pkg.plugin")
        ('pkg.plugin', None)
        >>> parse_entry_point_target("pkg.cli:") is None
        True
    """
    match = _TARGET_RE.fullmatch(value)
    if not match:
        return None
    module, attr = match.group("module"), match.group("attr")
    if not _is_dotted_identifier(module) or (attr is not None and not _is_dotted_identifier(attr)):
        return None
    return module, attr


def _classify(path: tuple[str, ...]) -> tuple[str, str] | None:
    """Map a full key path to ``(group, name)`` when it declares one entry point."""
    if len(path) == 3 and path[0] == _PROJECT and path[1] in _SCRIPT_GROUPS:
        return _SCRIPT_GROUPS[path[1]], path[2]
    if len(path) == 4 and path[:2] == (_PROJECT, _ENTRY_POINTS_KEY):
        return path[2], path[3]
    return None


def _line_entry_points(table: tuple[str, ...], line: str, lineno: int) -> list[EntryPoint]:
    """Return the entry points one in-scope assignment line declares."""
    found: list[EntryPoint] = []
    for path, value in _read_assignment(line):
        declared = _classify(table + path)
        target = parse_entry_point_target(value) if declared else None
        if declared and target:
            found.append(EntryPoint(declared[0], declared[1], target[0], target[1], lineno))
    return found


def parse_entry_points(text: str) -> list[EntryPoint]:
    """Return the entry points declared in ``pyproject.toml`` text, or none when the metadata is malformed.

    Args:
        text: full contents of a ``pyproject.toml`` file.

    Returns:
        Declared entry points in file order; an empty list when an in-scope line does not parse.

    Examples:
        >>> parse_entry_points('[project.scripts]\\ntool = "pkg.cli:main"\\n')
        [EntryPoint(group='console_scripts', name='tool', module='pkg.cli', symbol='main', line=2)]
        >>> parse_entry_points('[project.scripts]\\ntool = "pkg.cli:main\\n')
        []
    """
    try:
        return _collect_entry_points(text.splitlines())
    except _MalformedError:
        return []


def _collect_entry_points(lines: list[str]) -> list[EntryPoint]:
    """Walk the lines table by table, raising ``_MalformedError`` on unreadable in-scope text."""
    found: list[EntryPoint] = []
    table: tuple[str, ...] = ()
    pending = _NOTHING_OPEN
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if pending.is_open:
            pending = _advance_open_value(line, pending)
        elif not stripped or stripped.startswith("#"):
            continue
        elif stripped.startswith("["):
            table = _next_table(line, table)
        elif _line_in_scope(table, line):
            found.extend(_line_entry_points(table, line, lineno))
        else:
            pending = _advance_open_value(line, _NOTHING_OPEN)
    if pending.is_open:
        raise _MalformedError("file ends inside a multi-line string or array")
    return found


def scan_entry_point_refs(root: Path, module_names: set[str]) -> dict[str, list[dict]]:
    """Map indexed modules to the entry points the root ``pyproject.toml`` declares for them.

    Targets whose module is not in *module_names* are dropped. A symlinked, missing, unreadable or oversized
    ``pyproject.toml`` contributes nothing, matching the other root-config readers.

    Args:
        root: project root path.
        module_names: dotted names of the indexed modules.

    Returns:
        Dict mapping module name to ``{"group", "name", "symbol", "file", "line"}`` records in file order.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     _ = (Path(tmp) / "pyproject.toml").write_text('[project.scripts]\\ntool = "pkg.cli:main"\\n')
        ...     scan_entry_point_refs(Path(tmp), {"pkg.cli"})["pkg.cli"][0]["symbol"]
        'main'
    """
    path = root / "pyproject.toml"
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        if path.stat().st_size > discovery._MAX_FILE_SIZE_BYTES:
            return {}
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    refs: dict[str, list[dict]] = {}
    for entry in parse_entry_points(text):
        if entry.module in module_names:
            refs.setdefault(entry.module, []).append(
                {
                    "group": entry.group,
                    "name": entry.name,
                    "symbol": entry.symbol,
                    "file": "pyproject.toml",
                    "line": entry.line,
                }
            )
    return refs
