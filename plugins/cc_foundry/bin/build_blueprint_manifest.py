#!/usr/bin/env python
"""build_blueprint_manifest.py — hash every verbatim bash blueprint a plugin ships.

Plugin skills, agents and rules ship fenced ``bash`` blocks that the model is expected
to run **verbatim**. This script walks those Markdown files per plugin, normalizes every
bash block, and records a SHA-256 digest for the whole block and for each logical command
inside it. A companion PreToolUse hook hashes the command Claude Code is about to run and
looks it up here: an exact hit means "this text exists verbatim in a reviewed, versioned
plugin file" and may be auto-allowed; anything else falls through to the normal prompt.

Governing invariant
-------------------
``normalize(text stored in the manifest) == normalize(text the model sends verbatim)``

Every rule below follows from that invariant, and the runtime hook must implement this
pipeline **byte-identically**. This docstring is the single source of truth; the hook
copies it. A hook that normalizes *more* aggressively than this generator is the one real
bug class here — it lets a crafted command collide with a manifest digest.

Normalization pipeline (applied to a block, and to each logical command)
-----------------------------------------------------------------------
a. ``\\r\\n`` and lone ``\\r`` become ``\\n``; every line is right-stripped of whitespace.
c. Trailing ``#`` comments are removed, quote-aware, and only where ``#`` starts a word —
   i.e. it is at line start or directly preceded by a space/tab. Consequences, all
   deliberate: ``http://x#y`` survives, ``${V:-#x}`` survives, a ``#`` inside single or
   double quotes survives, and ``\\#`` survives. Quote state is tracked **across lines**,
   so a ``#`` inside a multi-line quoted string is left alone.
b. Leading and trailing blank lines are dropped and internal blank runs collapse to one.
d. No intra-line whitespace collapsing and no quote rewriting — two commands that differ
   only in spacing or quoting must never collide.

Steps are applied in the order a → c → b, i.e. comment stripping runs before blank
collapsing. This is exactly equivalent to the lettered order with (b) re-run after (c),
because blank lines carry no quote state. The fate of a comment-only line is therefore
fixed and must be matched by the hook: it becomes an empty line, and that empty line is
then folded by the blank collapse — a lone comment line between two commands leaves
exactly one blank line, and a leading or trailing comment line leaves nothing.

Known accepted edge case: comment stripping is applied per physical line, before
backslash-continuation joining, while real bash decides comment-vs-word status only
after splicing continued lines together. A raw ``xargs`` line ending in a line-continuation
backslash, followed by ``#rm -rf /`` on the next line, is treated here as a trailing
comment on an empty continuation, while bash would see the fused
token ``xargs#rm`` (not a comment, since ``#`` is glued to ``xargs`` with no separating
whitespace) and fail to parse it as a command at all. Verified (2026-08, Codex adversarial
pass) that this divergence cannot hide a *cleanly invocable* dangerous command: every
construction fuses the "hidden" text onto the tail of the preceding token, which breaks
both the head-command and argument-scan checks the same way it breaks bash's own parse.
Left unfixed deliberately — tracked debt, not a bypass.

Logical commands
----------------
A block is split into logical commands on **top-level newlines only** — never on ``;``,
``&&`` or ``|``, since a fragment lifted out of a compound command changes meaning. Lines
joined by a trailing ``\\`` continuation stay in the same logical command, and the
backslash-newlines are kept **verbatim** in the hashed text: the model sends the block as
written, so a spliced-together command would never match at runtime.

Conservative bail-out
---------------------
If a block contains ``<<`` anywhere, or any line ends with an unterminated quote, only the
whole-block entry is emitted and per-command extraction is skipped — line splitting is not
sound across a heredoc or a multi-line string. ``<<<`` and a quoted ``<<`` over-trigger
this; over-bailing only costs coverage, so it is the safe direction.

Danger filter
-------------
An entry is dropped when any of its segments — split on unquoted ``;`` ``&`` ``|``
newline, and including the contents of every substitution as segments of their own —
runs a destructive command. A segment's command token is found after skipping leading
``VAR=value`` assignments and any directory component, so ``FOO=1 rm x`` and ``/bin/rm x``
are both caught, and ``VAR=$(rm -rf x)`` is caught through its substitution segment.

Three substitution forms are read, because bash expands all three and a command hidden
in any of them runs: ``$(...)`` and backticks at top level *and* inside double quotes
(``echo "$(rm -rf /)"`` executes the ``rm``), and ``<(...)``/``>(...)`` process
substitution at top level. Single quotes are the one place bash expands nothing, so
``echo '$(rm -rf /)'`` is correctly left alone — reading a substitution there would
over-block a literal string.

A head-token-only check is not enough, because a whole family of commands carries the
real command in its arguments: ``eval rm -rf /``, ``find … | xargs rm -rf``,
``trap 'rm -f "$t"' EXIT``, ``sudo rm``, ``bash -c 'rm …'``, ``find … -delete``. When
the head token is one of :data:`DEFERRING_COMMANDS`, the arguments are scanned for a
destructive command too. Argument scanning ignores quoting, so ``trap 'rm …'`` is
caught; a plain ``echo 'rm -rf x'`` stays safe because ``echo`` does not defer.

The git check is a deliberate superset: any ``push``/``commit``/``reset``/``revert``
token, any ``worktree`` + ``remove`` pair, and any ``--force``/``-f`` token anywhere in the
segment. Overshooting drops extra entries, which only costs coverage. The dropped count is
always reported on stderr.

A block that is itself dangerous still has its individual commands examined — each entry
is judged on its own text.

Determinism
-----------
Output must be byte-identical when regenerated on Linux, macOS and native Windows, because
a CI drift gate compares committed bytes. Files are therefore ordered by their POSIX form,
every ``src`` path is emitted through ``PurePath.as_posix()``, and the manifest is written
as **bytes** (never text mode, which would emit CRLF on Windows). When two different source
locations produce the same digest, the first in that deterministic order wins.

Usage:
    build_blueprint_manifest.py --update [--scan-dir DIR]
    build_blueprint_manifest.py --check [--scan-dir DIR]

Options:
    --check             Compare committed manifests against a fresh build; write nothing.
    --update            Write the manifest for every scanned plugin.
    --scan-dir DIR      Directory holding the plugin directories (default: plugins).

Output:
    ``<scan-dir>/<plugin>/blueprint-manifest.json`` per plugin::

        {"schema": 1, "plugin": "cc_foundry@0.46.2",
         "entries": {"<sha256>": {"kind": "block", "src": "skills/audit/SKILL.md:120"}}}

    ``src`` is provenance, not an exact locator: command entries point at the opening
    fence line of the block they came from.

Exit codes:
    0   success (``--update`` wrote or confirmed; ``--check`` found no drift)
    1   ``--check`` found drift, or a target plugin directory is missing
    2   argument error (argparse default)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_code_blocks import iter_md_files, parse_blocks

#: Plugins that ship a blueprint manifest. ``codemap-py`` is excluded on purpose: its
#: hooks are Python-only by contract, so it ships no Node PreToolUse hook to consume one.
TARGET_PLUGINS: tuple[str, ...] = ("cc_foundry", "cc_oss", "cc_develop", "cc_research")

#: Subtrees walked for Markdown inside each plugin, plus the plugin-root files.
SCAN_SUBDIRS: tuple[str, ...] = ("skills", "agents", "rules")
SCAN_ROOT_FILES: tuple[str, ...] = ("CLAUDE.md",)

MANIFEST_NAME = "blueprint-manifest.json"
SCHEMA_VERSION = 1

#: Commands that are destructive enough that no auto-allow is ever warranted.
DANGER_COMMANDS: frozenset[str] = frozenset({"rm", "dd", "chmod", "chown", "mkfs", "shutdown", "kill", "pkill"})
#: Commands that carry another command in their arguments, so the destructive token is
#: never in head position — ``eval rm -rf /``, ``find … | xargs rm -rf`` and
#: ``trap 'rm -f "$t"' EXIT`` all leak past a head-token-only check.
DEFERRING_COMMANDS: frozenset[str] = frozenset(
    {
        "eval",
        "xargs",
        "sudo",
        "doas",
        "env",
        "nohup",
        "time",
        "timeout",
        "nice",
        "ionice",
        "command",
        "exec",
        "watch",
        "trap",
        "find",
        "bash",
        "sh",
        "zsh",
    }
)
#: git subcommands that mutate history, a remote, or the working tree.
GIT_DANGER_TOKENS: frozenset[str] = frozenset({"push", "commit", "reset", "revert"})
GIT_FORCE_TOKENS: frozenset[str] = frozenset({"--force", "--force-with-lease", "-f"})

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEGMENT_SEPARATORS = ";&|\n"

# Guard against pathological inputs that would exhaust heap memory when read in one
# shot. 10 MB is well above any realistic Markdown file.
_MAX_FILE_SIZE = 10 * 1024 * 1024


@dataclass(frozen=True)
class PluginBuild:
    """One plugin's freshly built manifest, ready to compare or write.

    Attributes:
        name: Plugin directory name (e.g. ``cc_foundry``).
        path: Path of the manifest file this build belongs to.
        payload: Encoded manifest bytes, exactly as they should appear on disk.
        entry_count: Number of digests in the manifest.
        dropped: Number of entries removed by the danger filter.
    """

    name: str
    path: Path
    payload: bytes
    entry_count: int
    dropped: int


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def strip_line_comment(line: str, quote: str | None = None) -> tuple[str, str | None]:
    """Remove a word-start ``#`` comment from one line, tracking quote state.

    The incoming ``quote`` is the open-quote character carried over from the previous
    line (``None`` when nothing is open). A ``#`` only starts a comment when it is
    unquoted, unescaped, and at line start or directly preceded by a space or tab.

    Args:
        line: A single line, already free of line terminators.
        quote: Quote character left open by the previous line, or None.

    Returns:
        Tuple of (line with any comment removed and right-stripped, quote state at the
        end of the line).

    Examples:
        >>> strip_line_comment("echo hi  # greet")
        ('echo hi', None)
        >>> strip_line_comment("curl http://x#y")
        ('curl http://x#y', None)
        >>> strip_line_comment('echo "${V:-#x}"')
        ('echo "${V:-#x}"', None)
        >>> strip_line_comment("echo '# not a comment'")
        ("echo '# not a comment'", None)
        >>> strip_line_comment("echo \\\\# literal")
        ('echo \\\\# literal', None)
        >>> strip_line_comment("# whole line")
        ('', None)
        >>> strip_line_comment('echo "open')
        ('echo "open', '"')
        >>> strip_line_comment('still # inside"', '"')
        ('still # inside"', None)
    """
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if quote is not None:
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index].rstrip(), quote
        index += 1
    return line.rstrip(), quote


def collapse_blank_lines(lines: list[str]) -> list[str]:
    """Drop leading/trailing blank lines and collapse internal blank runs to one.

    Args:
        lines: Right-stripped lines.

    Returns:
        New list with blank runs collapsed.

    Examples:
        >>> collapse_blank_lines(["", "a", "", "", "b", "", ""])
        ['a', '', 'b']
        >>> collapse_blank_lines(["", ""])
        []
        >>> collapse_blank_lines(["a"])
        ['a']
    """
    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1]:
            out.append("")
    while out and not out[-1]:
        out.pop()
    return out


def normalize(text: str) -> str:
    """Apply the full normalization pipeline to a bash block or command.

    See the module docstring for the authoritative spec; this is its implementation.

    Args:
        text: Raw block or command text.

    Returns:
        Normalized text (no trailing newline).

    Examples:
        >>> normalize("echo a\\r\\necho b   \\n")
        'echo a\\necho b'
        >>> normalize("\\n\\necho a\\n\\n\\n\\necho b\\n\\n")
        'echo a\\n\\necho b'
        >>> normalize("echo a  # why\\n# whole-line note\\necho b")
        'echo a\\n\\necho b'
        >>> normalize("curl http://x#y  # fetch")
        'curl http://x#y'
        >>> normalize("echo   'a  b'")
        "echo   'a  b'"
        >>> normalize("# only a comment")
        ''
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    quote: str | None = None
    stripped: list[str] = []
    for line in lines:
        cleaned, quote = strip_line_comment(line.rstrip(), quote)
        stripped.append(cleaned)
    return "\n".join(collapse_blank_lines(stripped))


def sha256_text(text: str) -> str:
    """Return the lowercase SHA-256 digest of ``text`` encoded as UTF-8.

    Args:
        text: Already-normalized text.

    Returns:
        64-character hex digest.

    Examples:
        >>> sha256_text("")[:16]
        'e3b0c44298fc1c14'
        >>> sha256_text("echo hi") == sha256_text("echo hi")
        True
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------


def _trailing_backslashes(line: str) -> int:
    """Count the backslashes ending ``line``.

    Args:
        line: Any line.

    Returns:
        Number of consecutive trailing backslash characters.

    Examples:
        >>> _trailing_backslashes("a \\\\")
        1
        >>> _trailing_backslashes("a \\\\\\\\")
        2
        >>> _trailing_backslashes("a")
        0
    """
    count = 0
    while count < len(line) and line[len(line) - 1 - count] == "\\":
        count += 1
    return count


def split_logical_commands(normalized: str) -> list[str]:
    """Split normalized block text into logical commands on top-level newlines.

    Lines held together by an odd number of trailing backslashes stay in one command,
    with the backslash-newlines preserved verbatim. Blank separator lines are dropped.

    Args:
        normalized: Text already passed through :func:`normalize`.

    Returns:
        List of logical command strings in document order.

    Examples:
        >>> split_logical_commands("echo a\\necho b")
        ['echo a', 'echo b']
        >>> split_logical_commands("echo a && echo b\\necho c")
        ['echo a && echo b', 'echo c']
        >>> split_logical_commands("cmd \\\\\\n  --flag\\necho b")
        ['cmd \\\\\\n  --flag', 'echo b']
        >>> split_logical_commands("echo a\\n\\necho b")
        ['echo a', 'echo b']
        >>> split_logical_commands("")
        []
    """
    commands: list[str] = []
    buffer: list[str] = []
    for line in normalized.split("\n"):
        buffer.append(line)
        if _trailing_backslashes(line) % 2 == 1:
            continue
        joined = "\n".join(buffer)
        if joined.strip():
            commands.append(joined)
        buffer = []
    if buffer and "\n".join(buffer).strip():
        commands.append("\n".join(buffer))
    return commands


def needs_bailout(normalized: str) -> bool:
    """Report whether per-command extraction is unsound for this block.

    Args:
        normalized: Text already passed through :func:`normalize`.

    Returns:
        True when the block holds a heredoc marker or an unterminated quote.

    Examples:
        >>> needs_bailout("echo a\\necho b")
        False
        >>> needs_bailout("cat <<EOF\\nhi\\nEOF")
        True
        >>> needs_bailout('echo "line one\\nline two"')
        True
        >>> needs_bailout('echo "one line"')
        False
    """
    if "<<" in normalized:
        return True
    quote: str | None = None
    for line in normalized.split("\n"):
        _, quote = strip_line_comment(line, quote)
        if quote is not None:
            return True
    return False


def _read_delimited(text: str, start: int, open_char: str | None, close_char: str) -> tuple[str, int]:
    """Read a nesting-aware delimited run beginning at ``start``.

    Args:
        text: Full text.
        start: Index of the first character inside the delimiter.
        open_char: Character that increases nesting depth, or None when there is none.
        close_char: Character that closes the run.

    Returns:
        Tuple of (inner text, index just past the closing delimiter).

    Examples:
        >>> _read_delimited("$(echo hi) rest", 2, "(", ")")
        ('echo hi', 10)
        >>> _read_delimited("$(a $(b) c) x", 2, "(", ")")
        ('a $(b) c', 11)
        >>> _read_delimited("`ls` x", 1, None, "`")
        ('ls', 4)
        >>> _read_delimited("$(unclosed", 2, "(", ")")
        ('unclosed', 10)
    """
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if open_char is not None and char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start:index], index + 1
        index += 1
    return text[start:], len(text)


def _read_substitution(text: str, index: int) -> tuple[str, int] | None:
    """Read a ``$(...)`` or backtick command substitution opening at ``index``.

    Both forms are expanded by bash inside double quotes as well as at top level, so
    this is called from both scanning states.

    Args:
        text: Full text.
        index: Index of the candidate opening character.

    Returns:
        Tuple of (inner text, index just past the closing delimiter), or None when no
        command substitution opens at ``index``.

    Examples:
        >>> _read_substitution("$(rm -rf x) y", 0)
        ('rm -rf x', 11)
        >>> _read_substitution("`ls` y", 0)
        ('ls', 4)
        >>> _read_substitution("echo hi", 0) is None
        True
    """
    if text.startswith("$(", index):
        return _read_delimited(text, index + 2, "(", ")")
    if text[index] == "`":
        return _read_delimited(text, index + 1, None, "`")
    return None


def _read_process_substitution(text: str, index: int) -> tuple[str, int] | None:
    """Read a ``<(...)`` or ``>(...)`` process substitution opening at ``index``.

    Only the two-character opener triggers this; a bare ``<`` or ``>`` redirection is
    left alone and is not a segment separator.

    Args:
        text: Full text.
        index: Index of the candidate ``<`` or ``>``.

    Returns:
        Tuple of (inner text, index just past the closing parenthesis), or None when no
        process substitution opens at ``index``.

    Examples:
        >>> _read_process_substitution("cat <(rm -rf x)", 4)
        ('rm -rf x', 15)
        >>> _read_process_substitution("cat < file", 4) is None
        True
    """
    if text[index] in "<>" and text.startswith("(", index + 1):
        return _read_delimited(text, index + 2, "(", ")")
    return None


def _scan_top_level(text: str) -> tuple[list[int], list[str]]:
    """Locate unquoted segment separators and expanded substitution bodies.

    Command substitutions are read at top level and inside double quotes — bash expands
    them in both — while process substitutions are read at top level only. Nothing is
    read inside single quotes, where bash expands nothing.

    Args:
        text: A command or block.

    Returns:
        Tuple of (indices of top-level separator characters, inner text of each
        substitution found).

    Examples:
        >>> _scan_top_level("a; b")
        ([1], [])
        >>> _scan_top_level("echo 'a; b'")
        ([], [])
        >>> _scan_top_level("V=$(rm -rf x)")
        ([], ['rm -rf x'])
        >>> _scan_top_level("echo `ls`")
        ([], ['ls'])
        >>> _scan_top_level('echo "$(rm -rf x)"')
        ([], ['rm -rf x'])
        >>> _scan_top_level("echo '$(rm -rf x)'")
        ([], [])
        >>> _scan_top_level("cat <(rm -rf x)")
        ([], ['rm -rf x'])
    """
    separators: list[int] = []
    substitutions: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\" and quote == '"':
                index += 2
                continue
            read = _read_substitution(text, index) if quote == '"' else None
            if read is not None:
                substitutions.append(read[0])
                index = read[1]
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        read = _read_substitution(text, index) or _read_process_substitution(text, index)
        if read is not None:
            substitutions.append(read[0])
            index = read[1]
            continue
        if char in _SEGMENT_SEPARATORS:
            separators.append(index)
        index += 1
    return separators, substitutions


def split_segments(command: str) -> list[str]:
    """Split a command into unquoted segments, including substitution bodies.

    Args:
        command: A command or block.

    Returns:
        Non-empty segment strings; substitution bodies are appended as their own
        segments (recursively) so a hidden command inside ``$(...)``, ``"$(...)"``,
        a backtick pair, or ``<(...)`` is still judged.

    Examples:
        >>> split_segments("echo a && rm b")
        ['echo a ', ' rm b']
        >>> split_segments("echo 'a && b'")
        ["echo 'a && b'"]
        >>> split_segments("V=$(rm -rf x)")
        ['V=$(rm -rf x)', 'rm -rf x']
        >>> split_segments('echo "$(rm -rf x)"')
        ['echo "$(rm -rf x)"', 'rm -rf x']
        >>> split_segments("cat <(rm -rf x)")
        ['cat <(rm -rf x)', 'rm -rf x']
        >>> split_segments("a | b; c")
        ['a ', ' b', ' c']
    """
    separators, substitutions = _scan_top_level(command)
    parts: list[str] = []
    previous = 0
    for position in separators:
        parts.append(command[previous:position])
        previous = position + 1
    parts.append(command[previous:])
    segments = [part for part in parts if part.strip()]
    for inner in substitutions:
        segments.extend(split_segments(inner))
    return segments


# ---------------------------------------------------------------------------
# Danger filter
# ---------------------------------------------------------------------------


def segment_tokens(segment: str) -> list[str]:
    """Split a segment into bare tokens with grouping and quote characters removed.

    Args:
        segment: One unquoted segment.

    Returns:
        Whitespace-split tokens, each stripped of grouping punctuation and quotes.

    Examples:
        >>> segment_tokens(" ( rm -rf x )")
        ['rm', '-rf', 'x']
        >>> segment_tokens('git "push" origin')
        ['git', 'push', 'origin']
        >>> segment_tokens("")
        []
    """
    tokens: list[str] = []
    for raw in segment.split():
        token = raw.strip("(){}").strip("'\"")
        if token:
            tokens.append(token)
    return tokens


def command_head(tokens: list[str]) -> tuple[str, list[str]]:
    """Return a segment's command token and the arguments following it.

    Leading ``VAR=value`` assignments are skipped, and any directory component is
    dropped, so ``FOO=1 /bin/rm -rf x`` reports ``rm``.

    Args:
        tokens: Tokens of one segment, as returned by :func:`segment_tokens`.

    Returns:
        Tuple of (command token, remaining tokens). The token is empty when the
        segment holds nothing but assignments.

    Examples:
        >>> command_head(["echo", "hi"])
        ('echo', ['hi'])
        >>> command_head(["FOO=1", "rm", "-rf", "x"])
        ('rm', ['-rf', 'x'])
        >>> command_head(["/bin/rm", "-rf", "x"])
        ('rm', ['-rf', 'x'])
        >>> command_head(["VAR=value"])
        ('', [])
    """
    for index, token in enumerate(tokens):
        if _ASSIGNMENT.match(token):
            continue
        return token.rsplit("/", 1)[-1], tokens[index + 1 :]
    return "", []


def _git_is_dangerous(tokens: list[str]) -> bool:
    """Report whether a ``git`` segment mutates history, a remote, or the tree.

    Args:
        tokens: Tokens of the segment, as returned by :func:`segment_tokens`.

    Returns:
        True when the segment carries a mutating subcommand or a force flag.

    Examples:
        >>> _git_is_dangerous(["git", "status"])
        False
        >>> _git_is_dangerous(["git", "-C", "x", "push"])
        True
        >>> _git_is_dangerous(["git", "checkout", "--force", "main"])
        True
        >>> _git_is_dangerous(["git", "worktree", "remove", "wt"])
        True
    """
    present = set(tokens)
    if present & GIT_DANGER_TOKENS or present & GIT_FORCE_TOKENS:
        return True
    return "worktree" in present and "remove" in present


def _segment_is_dangerous(segment: str) -> bool:
    """Report whether one segment runs a destructive command.

    A segment is judged by its command token. When that token merely carries another
    command (``eval``, ``xargs``, ``sudo``, ``trap``, ``find`` and friends — see
    :data:`DEFERRING_COMMANDS`), the arguments are scanned too, because the real
    command sits there rather than in head position.

    Args:
        segment: One unquoted segment.

    Returns:
        True when the segment must keep its entry out of the manifest.

    Examples:
        >>> _segment_is_dangerous("echo hi")
        False
        >>> _segment_is_dangerous(" xargs rm -rf")
        True
        >>> _segment_is_dangerous("eval rm -rf /")
        True
        >>> _segment_is_dangerous("eval echo hi")
        False
        >>> _segment_is_dangerous("trap 'rm -f \\"$tmp\\"' EXIT")
        True
        >>> _segment_is_dangerous("find . -mtime +30 -delete")
        True
        >>> _segment_is_dangerous("find .reports -type d -mtime +30")
        False
        >>> _segment_is_dangerous("echo 'rm -rf x'")
        False
    """
    tokens = segment_tokens(segment)
    head, rest = command_head(tokens)
    if head in DANGER_COMMANDS:
        return True
    if head == "git":
        return _git_is_dangerous(tokens)
    if head not in DEFERRING_COMMANDS:
        return False
    if "-delete" in rest:
        return True
    if any(token.rsplit("/", 1)[-1] in DANGER_COMMANDS for token in rest):
        return True
    return "git" in rest and _git_is_dangerous(rest)


def is_dangerous(command: str) -> bool:
    """Report whether any segment of ``command`` is destructive.

    Args:
        command: A normalized block or logical command.

    Returns:
        True when the entry must be kept out of the manifest.

    Examples:
        >>> is_dangerous("echo hi")
        False
        >>> is_dangerous("rm -rf build")
        True
        >>> is_dangerous("echo a && rm b")
        True
        >>> is_dangerous("VAR=$(rm -rf x)")
        True
        >>> is_dangerous("find . -name '*.tmp' | xargs rm -f")
        True
        >>> is_dangerous("git status --short")
        False
        >>> is_dangerous("git push origin main")
        True
        >>> is_dangerous("echo 'rm -rf x'")
        False
        >>> is_dangerous("eval rm -rf /")
        True
        >>> is_dangerous("eval echo hi")
        False
        >>> is_dangerous("cat <(rm -rf /)")
        True
        >>> is_dangerous('echo "$(rm -rf /)"')
        True
        >>> is_dangerous("echo '$(rm -rf /)'")
        False
    """
    return any(_segment_is_dangerous(segment) for segment in split_segments(command))


# ---------------------------------------------------------------------------
# Manifest assembly
# ---------------------------------------------------------------------------


def posix_src(relative: PurePath, line: int) -> str:
    """Render a provenance pointer with a forward-slash path.

    Takes a ``PurePath`` rather than a string so a Windows-flavoured path can be proved
    to normalize on any host — ``PurePath`` is host-flavoured, and a backslash is a legal
    filename character on POSIX, so wrapping a string here would silently do nothing.

    Args:
        relative: Path relative to the plugin directory, in any path flavour.
        line: 1-based line number of the block's opening fence.

    Returns:
        ``<posix-path>:<line>``.

    Examples:
        >>> from pathlib import PurePosixPath, PureWindowsPath
        >>> posix_src(PurePosixPath("skills/audit/SKILL.md"), 12)
        'skills/audit/SKILL.md:12'
        >>> posix_src(PureWindowsPath(r"skills\\audit\\SKILL.md"), 12)
        'skills/audit/SKILL.md:12'
    """
    return f"{relative.as_posix()}:{line}"


def plugin_label(plugin_dir: Path) -> str:
    """Return the ``<dir-name>@<version>`` identity recorded in the manifest.

    The directory name is used, not ``plugin.json``'s ``name`` field: the manifest is
    addressed by install path, and the two differ (``cc_foundry`` vs ``foundry``).

    Args:
        plugin_dir: Path to a plugin directory.

    Returns:
        Identity string; the version is ``unknown`` when the manifest is unreadable.

    Examples:
        >>> plugin_label(Path("/nonexistent-plugin"))
        'nonexistent-plugin@unknown'
    """
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    version = "unknown"
    try:
        parsed = json.loads(manifest.read_text(encoding="utf-8"))
        version = str(parsed.get("version", "unknown"))
    except (OSError, json.JSONDecodeError, ValueError):
        version = "unknown"
    return f"{plugin_dir.name}@{version}"


def collect_md_files(plugin_dir: Path) -> list[str]:
    """List the Markdown files scanned for a plugin, ordered by POSIX path.

    Args:
        plugin_dir: Path to a plugin directory.

    Returns:
        File paths in a host-independent order.

    Examples:
        >>> collect_md_files(Path("/nonexistent-plugin"))
        []
    """
    found: list[str] = []
    for subdir in SCAN_SUBDIRS:
        target = plugin_dir / subdir
        if target.is_dir():
            found.extend(iter_md_files(str(target)))
    for name in SCAN_ROOT_FILES:
        candidate = plugin_dir / name
        if candidate.is_file():
            found.append(str(candidate))
    return sorted(found, key=lambda path: PurePath(path).as_posix())


def _read_markdown(filepath: str) -> str | None:
    """Read a Markdown file, returning None when it is oversized or unreadable."""
    try:
        if Path(filepath).stat().st_size > _MAX_FILE_SIZE:
            print(f"warning: skipping oversized file: {filepath}", file=sys.stderr)
            return None
        return Path(filepath).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"warning: could not read {filepath}: {exc}", file=sys.stderr)
        return None


def _record(entries: dict[str, dict[str, str]], text: str, src: str, kind: str) -> None:
    """Record one digest, keeping the first source that produced it."""
    entries.setdefault(sha256_text(text), {"kind": kind, "src": src})


def block_entries(normalized: str, src: str) -> tuple[dict[str, dict[str, str]], int]:
    """Build the manifest entries contributed by one normalized bash block.

    Args:
        normalized: Block text already passed through :func:`normalize`.
        src: Provenance pointer shared by every entry from this block.

    Returns:
        Tuple of (digest → record mapping, number of entries dropped as dangerous).

    Examples:
        >>> entries, dropped = block_entries("echo a\\necho b", "f.md:1")
        >>> sorted(record["kind"] for record in entries.values())
        ['block', 'command', 'command']
        >>> dropped
        0
        >>> entries, dropped = block_entries("rm -rf x", "f.md:1")
        >>> entries
        {}
        >>> dropped
        1
        >>> entries, dropped = block_entries("echo one", "f.md:1")
        >>> [record["kind"] for record in entries.values()]
        ['block']
        >>> entries, _ = block_entries("cat <<EOF\\nhi\\nEOF", "f.md:1")
        >>> [record["kind"] for record in entries.values()]
        ['block']
    """
    entries: dict[str, dict[str, str]] = {}
    dropped = 0
    if is_dangerous(normalized):
        dropped += 1
    else:
        _record(entries, normalized, src, "block")
    if needs_bailout(normalized):
        return entries, dropped
    commands = split_logical_commands(normalized)
    if len(commands) < 2:
        return entries, dropped
    for command in commands:
        if is_dangerous(command):
            dropped += 1
            continue
        _record(entries, command, src, "command")
    return entries, dropped


def build_plugin(plugin_dir: Path) -> tuple[dict[str, object], int]:
    """Build one plugin's manifest object.

    Args:
        plugin_dir: Path to a plugin directory.

    Returns:
        Tuple of (manifest object, number of entries dropped as dangerous).

    Examples:
        >>> manifest, dropped = build_plugin(Path("/nonexistent-plugin"))
        >>> manifest["schema"], manifest["entries"], dropped
        (1, {}, 0)
    """
    entries: dict[str, dict[str, str]] = {}
    dropped = 0
    for filepath in collect_md_files(plugin_dir):
        text = _read_markdown(filepath)
        if text is None:
            continue
        relative = PurePath(filepath).relative_to(PurePath(plugin_dir))
        for block in parse_blocks(text, filepath):
            if block.lang_detected != "bash":
                continue
            normalized = normalize(block.content)
            if not normalized:
                continue
            found, block_dropped = block_entries(normalized, posix_src(relative, block.line_start))
            dropped += block_dropped
            for digest, record in found.items():
                entries.setdefault(digest, record)
    return {"schema": SCHEMA_VERSION, "plugin": plugin_label(plugin_dir), "entries": entries}, dropped


def encode_manifest(manifest: dict[str, object]) -> bytes:
    """Encode a manifest to the exact bytes written to disk.

    Args:
        manifest: Manifest object.

    Returns:
        UTF-8 bytes with a trailing newline, sorted and ASCII-escaped so the encoding is
        identical on every host.

    Examples:
        >>> encode_manifest({"schema": 1, "entries": {}})
        b'{\\n  "entries": {},\\n  "schema": 1\\n}\\n'
    """
    return (json.dumps(manifest, indent=2, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8")


def write_manifest(path: Path, payload: bytes) -> None:
    """Write manifest bytes atomically, replacing any existing file.

    Args:
        path: Destination manifest path.
        payload: Encoded manifest bytes.
    """
    temporary = path.with_suffix(".json.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_all(scan_dir: Path) -> tuple[list[PluginBuild], list[str]]:
    """Build every target plugin found under ``scan_dir``.

    Args:
        scan_dir: Directory holding the plugin directories.

    Returns:
        Tuple of (builds in TARGET_PLUGINS order, names of missing plugin directories).

    Examples:
        >>> builds, missing = build_all(Path("/nonexistent-scan-dir"))
        >>> builds
        []
        >>> missing == list(TARGET_PLUGINS)
        True
    """
    builds: list[PluginBuild] = []
    missing: list[str] = []
    for name in TARGET_PLUGINS:
        plugin_dir = scan_dir / name
        if not plugin_dir.is_dir():
            missing.append(name)
            continue
        manifest, dropped = build_plugin(plugin_dir)
        entries = manifest["entries"]
        builds.append(
            PluginBuild(
                name=name,
                path=plugin_dir / MANIFEST_NAME,
                payload=encode_manifest(manifest),
                entry_count=len(entries) if isinstance(entries, dict) else 0,
                dropped=dropped,
            )
        )
    return builds, missing


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]`` when None).

    Returns:
        Process exit code: 0 clean, 1 on drift or a missing plugin directory.
    """
    # Windows console/pipe default encoding is the locale codepage (often cp1252), which
    # cannot encode the U+2713 checkmark this CLI prints — reconfigure explicitly rather
    # than relying on PYTHONUTF8 being set by the caller. Streams captured by pytest lack
    # reconfigure(), so guard with hasattr instead of assuming a real TextIOWrapper.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        prog="build_blueprint_manifest",
        description="Hash every verbatim bash blueprint a plugin ships into a per-plugin manifest.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Fail when a committed manifest differs from a fresh build.")
    mode.add_argument("--update", action="store_true", help="Write the manifest for every scanned plugin.")
    parser.add_argument(
        "--scan-dir",
        default="plugins",
        metavar="DIR",
        help="Directory holding the plugin directories (default: plugins).",
    )
    args = parser.parse_args(argv)

    builds, missing = build_all(Path(args.scan_dir))
    for name in missing:
        print(f"error: plugin directory not found: {Path(args.scan_dir) / name}", file=sys.stderr)

    drift: list[str] = []
    for build in builds:
        print(
            f"{build.name}: {build.entry_count} entries, {build.dropped} dropped by danger filter",
            file=sys.stderr,
        )
        current = build.path.read_bytes() if build.path.is_file() else None
        if args.check:
            if current != build.payload:
                drift.append(build.name)
            continue
        if current != build.payload:
            write_manifest(build.path, build.payload)
            print(f"updated {build.path}")
        else:
            print(f"current {build.path}")

    if drift:
        print(f"BLUEPRINT-MANIFEST-DRIFT: {', '.join(drift)} — rerun with --update", file=sys.stderr)
        return 1
    if missing:
        return 1
    if args.check:
        print(f"✓: blueprint manifests current ({len(builds)} plugins)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
