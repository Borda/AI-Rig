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

Escaped blanks are refused before normalization (:func:`aliases_escaped_blank`). A backslash
followed by a blank is a quoted blank to the shell: at a line end the newline still ends the
command, and before ``#`` the rest of the line is an argument. Right-stripping and comment
removal would erase both, so the raw text and its normalized form would be different commands
sharing one digest. Such text gets no entry here and no allow in the hook; no shipped block
contains it.

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
real command in its arguments: ``find … | xargs rm -rf``, ``trap 'rm -f "$t"' EXIT``,
``sudo rm``, ``noglob rm``, ``bash -c 'rm …'``, ``find … -delete``. When the head token is
one of :data:`DEFERRING_COMMANDS`, the arguments are scanned for a destructive command too.
Argument scanning ignores quoting, so ``trap 'rm …'`` is caught; a plain ``echo 'rm -rf x'``
stays safe because ``echo`` does not defer. Command words are compared as the shell runs
them, with backslashes and quotes removed, so ``\\rm`` and ``r""m`` are ``rm``.

Rebinding a command name is dangerous too: a later ``ruff -rf x`` that looks harmless runs
``rm`` once ``ruff`` points at it. :data:`REBIND_COMMANDS` (``hash``, ``builtin``, ``alias``,
``enable``, ``functions``, ``eval`` and the rest) is denied by name in head position and among
a deferring command's arguments, since no shipped block runs one. So is any mention of a
table that maps a name to what it runs (zsh ``commands``, ``functions``, ``aliases``, bash
``BASH_CMDS`` …), an indirect expansion (``${(P)n}``, ``${!n}``), ``set -A``, ``read -a``/
``read -A`` or a ``read`` into a name built from an expansion, ``printf -v``/``print -v``, and a
declaring builtin's name reference or expansion-built name (``declare -n``, ``typeset "$n=…"``).

The command word is found after skipping grouping punctuation, shell reserved words
(``then rm x`` runs ``rm``) and leading assignments, including a value that spans several
words (``FOO="a b" rm x``, ``V=$(a b) rm x``). A command word that is itself an expansion
(``$CMD``, ``${CMD}``, ``$(...)``) is dangerous: bash runs whatever it yields, which no static
filter can judge. ``$(( ... ))`` arithmetic is not read as a command, but a body whose first
paren closes early (``$((rm x); true)``) is a command substitution and is still judged, and a
``[[ ... ]]`` test is kept as one segment so its ``&&``/``||`` do not leave a bare ``$VAR``
standing as a command.

The git check is a deliberate superset: any ``push``/``commit``/``reset``/``revert``/
``restore``/``clean`` token, any ``worktree`` + ``remove`` pair, ``checkout`` with ``--`` or
``.``, ``branch`` with ``-D``/``-d``/``--delete``, ``stash`` with ``drop``/``clear``, and any
``--force``/``-f`` token anywhere in the segment. ``sed`` with ``-i``/``--in-place``, ``gh api``
with a ``DELETE`` method, and a shell that reads its script from stdin (``curl … | sh``) are
dangerous too. Overshooting drops extra entries, which only costs coverage. The dropped count
is always reported on stderr.

:func:`is_dangerous` stays a denylist on purpose: ``hooks/blueprint-allow.js`` re-runs the
same check on every digest hit, and the shared vector fixture pins the two to agree, so the
manifest-side tightening below lives in this generator only.

Allowlist gate
--------------
An entry the denylist passes is admitted only when every command it runs is on an allowlist
(:func:`unlisted_command`): fail-closed, so a command word nobody listed — ``ln``, ``perl``,
``strace``, an unknown CLI — keeps the entry out, while the reviewed shapes shipped blocks rely
on stay admitted. :data:`SAFE_COMMANDS` holds shell builtins and keywords, utilities that only
read, tools that create or check without destroying (``mkdir``, ``touch``, ``pytest``,
``ruff`` …) and ``tee``. :data:`_ALLOWLIST_CHECKS` admits particular uses of a few more:
``python`` running a script (one whose name marks it as committing, deleting, reverting or
unlinking only when :data:`_MUTATING_HELPERS` lists it with its guard), a listed ``-m`` module,
``-`` fed by a quoted-delimiter heredoc or ``-c`` with code fixed in the text (single-quoted, or
double-quoted without ``$``/backtick), ``node`` running a script, ``awk`` without
``system()``/pipes/file writes, ``curl`` GET to stdout, ``find`` without deleting or
file-writing actions, ``cp`` except onto or out of ``/`` or ``$HOME`` itself, ``unlink`` of a
state path, ``bash`` of a plugin script or a state-path script named in :data:`_STATE_SCRIPTS`,
``kaggle`` listing verbs, ``mv`` as a consume-marker rename (``mv "$F" "$F.done"``), and the
wrappers ``xargs``, ``timeout``, ``env``, ``command``, ``nice``, ``nohup``, ``time``,
``uv run`` and ``find -exec``, whose wrapped command is judged in turn. A state path
(:func:`_is_state_path`) is a ``${TMPDIR:-/tmp}/…${CSID}`` sentinel, a ``.temp/`` file, or a
path under a trusted variable.

``trap`` handlers and ``sh -c`` strings are judged as commands; ``eval`` never reaches this gate,
since the denylist drops it by name. A state file holds data an earlier step wrote, sometimes
from outside the skill, so nothing turns it into code: ``.``/``source`` is never admitted, and a
command word that is itself an expansion is unlisted even when the variable is trusted
(:func:`_trusted_variables` names path roots only), so ``bash -c "$METRIC_CMD"`` stays out.

Entry shape
-----------
A block with a data heredoc gets no entry (:func:`has_data_heredoc`): normalization strips
comments and trailing blanks inside heredoc bodies, which would let different data share one
digest; Python code read through a quoted delimiter (``python - <<'PY'``) is exempt, since
there the stripped text is inert. A line becomes an entry of its own only when it is a complete
command (:func:`is_complete_command`), so fragments such as ``if …; then`` or ``f() {`` cannot be
joined across blocks into a compound no shipped block contains.

The gate reads shell syntax the denylist's segment split leaves as words: ``case`` labels and
``a|b)`` pattern alternatives, function definitions and calls to them, ``NAME+=(…)`` appends,
redirections (``2>&1`` does not split; ``} > "$OUT"`` is no command), backslash-newline
continuations, heredoc bodies (data; only substitutions left live by an unquoted delimiter
are judged; an unterminated heredoc excludes the entry), and ``$(…)`` bodies read the bash way
(:func:`_read_command_body`: quoted parentheses and ``case`` pattern ``)`` do not close them),
masked out of the surrounding command and judged on their own.

:func:`is_dangerous` also judges the command a ``case`` label, an arm on the ``case … in``
line, a function definition or a zsh short or brace compound form puts behind a non-command
word, followed to its innermost command (:func:`_command_texts`), so ``case x in a) rm -rf /;; esac``
and ``f() { rm -rf x; }`` are dangerous; the rebind check and the allowlist gate read the same
texts, the JS twin mirrors this and the shared vector fixture pins both.

Known gaps, deliberate: ``git``, ``gh`` and ``sed`` are listed whole and rely on the denylist
for their destructive forms, so ``gh`` write subcommands other than ``api -X DELETE`` pass
here (``gh-write-guard.js`` gates them at runtime); output redirections and ``tee``/``cp``
targets are not judged, since every sentinel write relies on them; a no-argument command closing a subshell
inside a ``case`` arm (``(cd d; make)``) reads as a label.

A block that is itself excluded still has its individual commands examined — each entry
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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _blueprint_danger import (
    _ASSIGNMENT,
    SHELL_INTERPRETERS,
    _assignment_end,
    _case_arm_chain,
    _case_label_end,
    _case_line_arm,
    _command_words,
    _function_body,
    _function_name,
    _has_short_flag,
    _head_is_expansion,
    _heredoc_operators,
    _is_arithmetic,
    _is_case_alternative,
    _judged_texts,
    _read_gate_substitution,
    _read_process_substitution,
    _read_substitution,
    _scan_top_level,
    _split_heredocs,
    _top_level_segments,
    is_complete_command,
)
from _blueprint_danger import (
    DANGER_COMMANDS as DANGER_COMMANDS,
)
from _blueprint_danger import (
    DEFERRING_COMMANDS as DEFERRING_COMMANDS,
)
from _blueprint_danger import (
    GIT_BRANCH_DELETE_TOKENS as GIT_BRANCH_DELETE_TOKENS,
)
from _blueprint_danger import (
    GIT_DANGER_TOKENS as GIT_DANGER_TOKENS,
)
from _blueprint_danger import (
    GIT_FORCE_TOKENS as GIT_FORCE_TOKENS,
)
from _blueprint_danger import (
    REBIND_COMMANDS as REBIND_COMMANDS,
)
from _blueprint_danger import (
    SHELL_RESERVED_WORDS as SHELL_RESERVED_WORDS,
)
from _blueprint_danger import (
    command_head as command_head,
)
from _blueprint_danger import (
    is_dangerous as is_dangerous,
)
from _blueprint_danger import (
    segment_tokens as segment_tokens,
)
from _blueprint_danger import (
    shell_words as shell_words,
)
from _blueprint_danger import (
    split_segments as split_segments,
)
from extract_code_blocks import iter_md_files, parse_blocks

#: Plugins that ship a blueprint manifest. ``codemap-py`` is excluded on purpose: its
#: hooks are Python-only by contract, so it ships no Node PreToolUse hook to consume one.
TARGET_PLUGINS: tuple[str, ...] = ("cc_foundry", "cc_oss", "cc_develop", "cc_research")

#: Subtrees walked for Markdown inside each plugin, plus the plugin-root files.
SCAN_SUBDIRS: tuple[str, ...] = ("skills", "agents", "rules")
#: Plugin-root files, in addition to the scanned subdirectories, that are included in the manifest.
SCAN_ROOT_FILES: tuple[str, ...] = ("CLAUDE.md",)

#: File name of the generated manifest, written inside each plugin directory.
MANIFEST_NAME = "blueprint-manifest.json"
#: Schema revision stored in the manifest's ``schema`` field.
SCHEMA_VERSION = 1

# Guard against pathological inputs that would exhaust heap memory when read in one
# shot. 10 MB is well above any realistic Markdown file.
#: Files larger than this many bytes (10 MiB) are skipped when scanning.
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


#: An unescaped backslash, then a blank, then ``#``: the shell keeps ``\ #…`` as word text, normalization drops it.
_ESCAPED_BLANK_BEFORE_COMMENT = re.compile(r"(?<!\\)(?:\\\\)*\\[ \t]#")


def aliases_escaped_blank(text: str) -> bool:
    """Report whether normalization would erase a blank the shell reads as escaped.

    The shell reads a backslash followed by a blank as a quoted blank. At the end of a line that means the newline
    still ends the command, so the next line runs on its own; normalization strips the blank first and sees a
    continuation. Before ``#`` it means the blank and the rest of the line are one more argument; normalization sees a
    comment and drops them. Either way the raw text and its normalized form are different commands that share a digest,
    so the raw text is refused before any lookup. No shipped block contains either shape.

    Args:
        text: Raw block or command text, before normalization.

    Returns:
        True when the text must not be matched against the manifest.

    Examples:
        >>> aliases_escaped_blank("ls \\\\ \\necho x")
        True
        >>> aliases_escaped_blank("ls \\\\\\necho x")
        False
        >>> aliases_escaped_blank("ls \\\\\\\\ \\necho x")
        False
        >>> aliases_escaped_blank("echo a\\\\ #b")
        True
        >>> aliases_escaped_blank("echo a  # note")
        False
    """
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        trimmed = line.rstrip()
        if len(trimmed) < len(line) and _trailing_backslashes(trimmed) % 2 == 1:
            return True
        if _ESCAPED_BLANK_BEFORE_COMMENT.search(line):
            return True
    return False


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


def has_data_heredoc(normalized: str) -> bool:
    """Report whether a block opens a heredoc that carries data rather than Python code.

    Normalization strips ``#`` comments and trailing whitespace from every line, heredoc bodies included, so a command
    whose heredoc data differs from a shipped block only there would hash to that block's digest. A block holding such
    a heredoc gets no manifest entry at all (:func:`block_entries`). The one exception is code fed to the interpreter
    through a quoted delimiter (``python - <<'PY'``): there a stripped comment or trailing blank is inert, as it is in
    the shipped text.

    Args:
        normalized: Text already passed through :func:`normalize`.

    Returns:
        True when any line opens a ``<<WORD``, ``<<-WORD`` or ``<<'WORD'`` heredoc that is not Python code read from a
        quoted delimiter; a ``<<<`` here-string is not a heredoc.

    Examples:
        >>> has_data_heredoc("cat > f <<'EOF'\\nx\\nEOF")
        True
        >>> has_data_heredoc("python - \\"$F\\" <<'PY'\\nprint(1)\\nPY")
        False
        >>> has_data_heredoc("python - <<PY\\nprint('$X')\\nPY")
        True
        >>> has_data_heredoc('grep -n "python <<" f')
        False
        >>> has_data_heredoc('grep x <<< "$v"')
        False
    """
    return any(_heredoc_operators(line) and not _python_code_heredoc(line) for line in normalized.split("\n"))


def _python_code_heredoc(line: str) -> bool:
    """Report whether a line feeds Python code to the interpreter through quoted heredoc delimiters only.

    Examples:
        >>> _python_code_heredoc("python - \\"$F\\" <<'PY'")
        True
        >>> _python_code_heredoc("cat > f <<'PY'")
        False
    """
    words = _command_start(_command_words(line))
    reads_code = len(words) > 1 and words[0] in ("python", "python3") and words[1] == "-"
    return reads_code and all(quoted for _, quoted, _ in _heredoc_operators(line))


# ---------------------------------------------------------------------------
# Allowlist gate
# ---------------------------------------------------------------------------

#: Command words an entry may run with any arguments: shell builtins and keywords, utilities that only read, and
#: tools that create or check without destroying. ``git``, ``gh`` and ``sed`` are listed because
#: :func:`is_dangerous` already drops their destructive uses before this gate is asked.
SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        # shell builtins and keywords
        *("[", "[[", "test", ":", "true", "false", "read", "export", "exit", "return", "set", "unset", "setopt"),
        *("unsetopt", "shopt", "cd", "pwd", "if", "then", "elif", "else", "fi", "case", "esac", "for", "select"),
        *("while", "until", "do", "done", "continue", "break", "function", "local", "declare", "typeset"),
        *("readonly", "shift", "getopts", "printf", "echo", "type", "which", "wait"),
        # utilities that only read
        *("cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "sort", "uniq", "cut", "tr", "paste"),
        *("basename", "dirname", "realpath", "readlink", "ls", "stat", "date", "jq", "diff", "cmp", "comm"),
        *("cksum", "sha256sum", "shasum", "md5sum", "md5", "file", "du", "df", "column", "fold", "nl", "rev"),
        *("tac", "seq", "sleep", "uname", "whoami", "hostname", "id", "printenv", "od", "xxd", "tree", "expr", "bc"),
        # tools that create or check without destroying
        *("mkdir", "mktemp", "touch", "git", "gh", "sed", "codemap-py", "pre-commit", "ruff", "mypy", "pytest"),
        # writers shipped blocks use to append or log state files they name
        "tee",
    }
)
#: Modules ``python -m`` may run: test, lint and inspection entry points.
SAFE_PYTHON_MODULES: frozenset[str] = frozenset(
    {"pytest", "doctest", "json.tool", "cProfile", "pstats", "pre_commit", "mypy", "ruff"}
)
#: Flags that make an interpreter print information and exit without running code.
_INFO_FLAGS: frozenset[str] = frozenset({"--version", "-V", "-h", "--help"})
#: A word that starts with a redirection (``> "$OUT"`` after a group's closing brace, ``2>/dev/null``).
_REDIRECTION_HEAD = re.compile(r"\d*&?[<>]")
#: A redirection operator written apart from its target, which is then the next word.
_BARE_REDIRECTION = re.compile(r"\d*(?:>>?|<<?|>\||<>|&>>?)")
#: A ``NAME+=value`` append assignment, which :func:`_command_words` does not recognise.
_APPEND_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+=")
#: Awk constructs that run a command or write a file: ``system(``, ``"cmd" | getline``, ``print > f``/``print | c``.
_AWK_ESCAPE = re.compile(r"\bsystem\s*\(|\|\s*getline|\bprintf?\b[^;}]*[|>]")
#: ``find`` actions that delete or write files.
_FIND_WRITE_ACTIONS: frozenset[str] = frozenset({"-delete", "-fprint", "-fprint0", "-fprintf", "-fls"})
#: ``find`` actions that run a command; the command is judged like any other.
_FIND_EXEC_ACTIONS: frozenset[str] = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
#: Words that end a ``find -exec`` command.
_EXEC_TERMINATORS: frozenset[str] = frozenset({";", "\\;", "';'", '";"', "+"})
#: A ``read`` command: its variable names, then the rest of the line (where a ``<`` source would follow).
_READ_TARGETS = re.compile(r"(?<![\w$-])read[ \t]+(?:-[A-Za-z]+[ \t]+)*((?:[A-Za-z_][A-Za-z0-9_]*[ \t]*)+)(.*)")
#: The file a ``read`` redirects from, optionally double-quoted.
_READ_SOURCE = re.compile(r"""[ \t]*<[ \t]*("?)([^"\s]+)\1""")
#: A ``NAME=value`` assignment word and its value.
_ASSIGNMENT_WORD = re.compile(
    r"(?:(?<=[ \t;&|(!{])|^)([A-Za-z_][A-Za-z0-9_]*)="
    r"(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\$\([^)]*\)|[^ \t;&|)'\"]*)",
    re.MULTILINE,
)
#: A ``for NAME in`` loop variable.
_FOR_VARIABLE = re.compile(r"(?<![\w$-])for[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]+in\b")
#: A ``$(mktemp …)`` call with fixed arguments.
_MKTEMP_CALL = re.compile(r"\$\(mktemp(?:[ \t][^()$`]*)?\)")
#: A leading ``$NAME`` or ``${NAME}``, alone or followed by ``/``: the root of a path built on a variable.
_VARIABLE_PREFIX = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))(?:/|$|\.[A-Za-z0-9_-]+$)")
#: Any ``$NAME`` or ``${NAME…}`` reference in a path word, capturing the name.
_VARIABLE_NAME = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
#: Variables a session state path may name without the entry having set them: the temp root and the session token.
_STATE_PATH_VARIABLES = frozenset({"$TMPDIR", "$CSID"})
#: Operands ``cp`` may never take: the filesystem root or the home directory itself.
_ROOT_PATHS: frozenset[str] = frozenset({"/", "/*", "~", "~/", "$HOME", "${HOME}", "$HOME/", "${HOME}/"})
#: ``kaggle`` command groups and the listing verbs under them that print and write nothing.
_KAGGLE_GROUPS: frozenset[str] = frozenset({"competitions", "datasets", "kernels", "models"})
_KAGGLE_READ_VERBS: frozenset[str] = frozenset({"list", "files", "leaderboard", "status"})
#: Suffix of a consume-marker rename target (``"$F"`` → ``"$F.done"``).
_MARKER_SUFFIX = re.compile(r"\.[A-Za-z0-9_-]+")
#: Long ``curl`` options that send data or write a local file.
_CURL_WRITE_PREFIXES: tuple[str, ...] = ("--data", "--form", "--upload-file", "--output", "--remote-name", "--json")
#: Options taking a separate value, per wrapper, so the wrapped command word is found after them.
_XARGS_VALUE_OPTIONS: frozenset[str] = frozenset({"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a"})
_TIMEOUT_VALUE_OPTIONS: frozenset[str] = frozenset({"-s", "-k", "--signal", "--kill-after"})
_ENV_VALUE_OPTIONS: frozenset[str] = frozenset({"-u", "-C", "-S"})
_UV_RUN_VALUE_OPTIONS: frozenset[str] = frozenset(
    {"--with", "--python", "-p", "--project", "--directory", "--extra", "--group", "--package", "--env-file"}
)
#: Options of ``uv run`` that take no value and cannot change which command runs.
_UV_RUN_FLAGS: frozenset[str] = frozenset(
    {
        "--frozen", "--locked", "--no-sync", "--isolated", "--active", "--no-project", "--offline",
        "--quiet", "-q", "--verbose", "-v", "--no-dev", "--all-extras", "--all-groups", "--no-default-groups",
        "--exact", "--no-editable", "--no-env-file",
    }
)  # fmt: skip


def _unquote_leading(text: str) -> str:
    """Return the body of the quoted string ``text`` opens with, or ``text`` when it opens with no quote.

    Args:
        text: Argument text of ``trap`` or ``sh -c``.

    Returns:
        The first quoted string's body without its quotes, or ``text`` unchanged.

    Examples:
        >>> _unquote_leading('"$RUNNER pytest -q" > log')
        '$RUNNER pytest -q'
        >>> _unquote_leading('"$(python "$ROOT/x.py")" 2>/dev/null')
        '$(python "$ROOT/x.py")'
        >>> _unquote_leading("'rm -f x' EXIT")
        'rm -f x'
        >>> _unquote_leading("echo hi")
        'echo hi'
    """
    stripped = text.lstrip()
    if not stripped or stripped[0] not in "'\"":
        return text
    quote = stripped[0]
    index = 1
    while index < len(stripped):
        read = _read_substitution(stripped, index) if quote == '"' else None
        if read is not None:
            index = read[1]
            continue
        if stripped[index] == "\\" and quote == '"':
            index += 2
            continue
        if stripped[index] == quote:
            return stripped[1:index]
        index += 1
    return stripped[1:]


def _skip_options(args: list[str], with_value: frozenset[str]) -> int:
    """Return the index of the first word after a wrapper's options.

    Examples:
        >>> _skip_options(["-I", "{}", "basename", "{}"], frozenset({"-I"}))
        2
        >>> _skip_options(["-0", "-r", "grep", "x"], frozenset())
        2
    """
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in with_value else 1
    return index


def _python_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge a ``python`` invocation: a script file, a listed ``-m`` module, or code fixed in the shipped text.

    ``python - <<'PY'`` reads a heredoc whose quoted delimiter keeps bash from expanding anything in it, and
    ``python -c '<code>'`` (or a double-quoted code string with no ``$`` or backtick) passes code bash leaves as
    written — either way the code is the verbatim reviewed text, a script file written inline. ``-c`` code built
    from an expansion, stdin piped from another command and an unquoted heredoc stay unlisted.

    Args:
        args: Raw words after the command word.
        known: Names known to the entry (unused; uniform check signature).

    Returns:
        The offending use, or None when allowed.

    Examples:
        >>> _python_use(['"$ROOT/bin/x.py"', "--flag"], frozenset()) is None
        True
        >>> _python_use(["-m", "pytest", "-q"], frozenset()) is None
        True
        >>> _python_use(["-", '"$F"', "<<'PY'"], frozenset()) is None
        True
        >>> _python_use(["-", "<<PY"], frozenset())
        'python -'
        >>> _python_use(["-c", "'import", "sys;", "print(1)'"], frozenset()) is None
        True
        >>> _python_use(["-c", '"$CODE"'], frozenset())
        'python -c'
        >>> _python_use(["-m", "pip", "install", "x"], frozenset())
        'python -m pip'
        >>> _python_use([], frozenset())
        'python'
        >>> _python_use(['"$ROOT/bin/purge_plugin_cache.py"', "--apply"], frozenset()) is None
        True
        >>> _python_use(['"$ROOT/bin/delete_reports.py"'], frozenset())
        'mutating helper delete_reports.py'
    """
    index = 0
    while index < len(args):
        word = args[index].strip("'\"")
        if word in _INFO_FLAGS:
            return None
        if word == "-m":
            module = args[index + 1].strip("'\"") if index + 1 < len(args) else ""
            return None if module in SAFE_PYTHON_MODULES else f"python -m {module}".rstrip()
        if word == "-":
            heredocs = _heredoc_operators(" ".join(args))
            return None if heredocs and all(quoted for _, quoted, _ in heredocs) else "python -"
        if _has_short_flag([word], "c"):
            return None if _is_literal_code(args[index + 1 :]) else f"python {word}"
        if not word.startswith("-"):
            return _helper_use(word)
        index += 2 if word in ("-W", "-X") else 1
    return "python"


#: A script whose file name says it commits, deletes, reverts or unlinks something.
_MUTATING_HELPER = re.compile(
    r"^(?:commit|purge|remove|revert|delete|reset|prune|unlink|wipe|drop|uninstall)_|symlink|clean"
)
#: Mutating plugin helpers the manifest admits, each with the guard that bounds what it changes. The danger check sees
#: only the command line, never what a script does, so a helper whose name matches :data:`_MUTATING_HELPER` stays out
#: of the manifest until it is listed here with the reason its effect is safe to auto-allow.
_MUTATING_HELPERS = {
    "commit_action_item.py": "oss:resolve per-item commit: stages only the files it is given, never pushes",
    "commit_all_items.py": "oss:resolve bulk commit of the resolved items, never pushes",
    "commit_lint_fixes.py": "commits only lint changes inside the branch's own changed files, never pushes",
    "heal_git_artifacts.py": (
        "reclaims only provably stale skill locks and managed-prefix worktrees: dead-PID plus age guard for locks;"
        " a worktree holding uncommitted work is reported, never removed; orphans only with --include-orphans"
    ),
    "merge_specialist_batch.py": (
        "oss:resolve: cherry-picks the run's own per-item commits onto the branch; its only reset is --soft back to"
        " the base it recorded, so no work is discarded; a conflict leaves applied commits in place"
    ),
    "purge_plugin_cache.py": "deletes only orphaned plugin cache versions; --apply is opt-in, --protect keeps live ones",
    "remove_hook_from_registry.py": "drops one named hook from a hooks registry JSON file",
    "revert_iteration_commit.py": "reverts HEAD only when its subject is this iteration's experiment commit",
    "symlink_with_guard.py": "removes only foundry-managed symlinks whose targets no longer exist",
}


def _helper_use(script: str) -> str | None:
    """Judge a Python script operand: a mutating helper must be listed in :data:`_MUTATING_HELPERS`.

    Examples:
        >>> _helper_use("$ROOT/bin/agent_watch.py") is None
        True
        >>> _helper_use("$ROOT/bin/symlink_with_guard.py") is None
        True
        >>> _helper_use("$ROOT/bin/remove_cache.py")
        'mutating helper remove_cache.py'
    """
    name = PurePath(script).name
    if _MUTATING_HELPER.search(name) and name not in _MUTATING_HELPERS:
        return f"mutating helper {name}"
    return None


def _is_literal_code(args: list[str]) -> bool:
    """Report whether an inline code argument is a quoted string bash passes through unexpanded.

    Examples:
        >>> _is_literal_code(["'print($x)'"])
        True
        >>> _is_literal_code(['"print(1)"'])
        True
        >>> _is_literal_code(['"print($X)"'])
        False
        >>> _is_literal_code(["print"])
        False
    """
    text = " ".join(args).lstrip()
    if not text or text[0] not in "'\"":
        return False
    code = _unquote_leading(text)
    return text[0] == "'" or not any(char in code for char in "$`")


def _node_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge a ``node`` invocation: a script file, never ``-e``/``-p`` inline code or stdin.

    Examples:
        >>> _node_use(["--check", "hook.js"], frozenset()) is None
        True
        >>> _node_use(["-e", "'process.exit(0)'"], frozenset())
        'node -e'
        >>> _node_use([], frozenset())
        'node'
    """
    for raw in args:
        word = raw.strip("'\"")
        if word in _INFO_FLAGS or word == "-v":
            return None
        if word in ("-", "-e", "--eval", "-p", "--print") or word.startswith(("--eval=", "--print=")):
            return f"node {word}"
        if not word.startswith("-"):
            return None
    return "node"


def _awk_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge an ``awk`` program: reading and printing only, never ``system()``, pipes or file writes.

    Examples:
        >>> _awk_use(["-F'\\\\t'", "'{print $2}'"], frozenset()) is None
        True
        >>> _awk_use(["'{system(\\"rm \\" $1)}'"], frozenset())
        'awk system/pipe/write'
        >>> _awk_use(["'{print $1 > \\"out\\"}'"], frozenset())
        'awk system/pipe/write'
    """
    return "awk system/pipe/write" if _AWK_ESCAPE.search(" ".join(args)) else None


def _curl_method(tokens: list[str], index: int) -> str | None:
    """Return the HTTP method a ``curl`` token sets, or None when it sets none.

    Examples:
        >>> _curl_method(["-X", "POST"], 0)
        'POST'
        >>> _curl_method(["--request=DELETE"], 0)
        'DELETE'
        >>> _curl_method(["-s"], 0) is None
        True
    """
    token = tokens[index]
    if token in ("-X", "--request"):
        return tokens[index + 1] if index + 1 < len(tokens) else ""
    if token.startswith("--request="):
        return token.removeprefix("--request=")
    if token.startswith("-X") and len(token) > 2:
        return token[2:]
    return None


def _curl_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge a ``curl`` call: a GET to stdout, never another method, a request body, an upload or a file write.

    Examples:
        >>> _curl_use(["-sS", "https://example.com"], frozenset()) is None
        True
        >>> _curl_use(["-X", "POST", "-d", "x", "https://example.com"], frozenset())
        'curl -X POST'
        >>> _curl_use(["-o", "out.html", "https://example.com"], frozenset())
        'curl -o'
    """
    tokens = [arg.strip("'\"") for arg in args]
    for index, token in enumerate(tokens):
        method = _curl_method(tokens, index)
        if method is not None and method.upper() != "GET":
            return f"curl -X {method}"
        if token.startswith(_CURL_WRITE_PREFIXES) or any(_has_short_flag([token], flag) for flag in "dFToO"):
            return f"curl {token}"
    return None


def _find_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge a ``find`` call: no deleting or file-writing action, and every ``-exec`` command allowlisted.

    Examples:
        >>> _find_use([".", "-name", "'*.md'"], frozenset()) is None
        True
        >>> _find_use([".", "-exec", "grep", "-l", "x", "{}", "+"], frozenset()) is None
        True
        >>> _find_use([".", "-exec", "mv", "{}", "/tmp", "\\\\;"], frozenset())
        'mv'
        >>> _find_use([".", "-fprint", "out"], frozenset())
        'find -fprint'
    """
    index = 0
    while index < len(args):
        token = args[index].strip("'\"")
        if token in _FIND_WRITE_ACTIONS:
            return f"find {token}"
        if token in _FIND_EXEC_ACTIONS:
            end = index + 1
            while end < len(args) and args[end] not in _EXEC_TERMINATORS:
                end += 1
            offender = _words_unlisted(args[index + 1 : end], known)
            if offender is not None:
                return offender
            index = end
        index += 1
    return None


def _xargs_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge the command ``xargs`` runs; with none it runs ``echo``.

    Examples:
        >>> _xargs_use(["-0", "-r", "grep", "-l", "x"], frozenset()) is None
        True
        >>> _xargs_use(["-I", "{}", "mv", "{}", "dst"], frozenset())
        'mv'
    """
    return _words_unlisted(args[_skip_options(args, _XARGS_VALUE_OPTIONS) :], known)


def _timeout_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge the command ``timeout`` runs, after its options and duration.

    Examples:
        >>> _timeout_use(["5", "curl", "-sS", "https://example.com"], frozenset()) is None
        True
        >>> _timeout_use(["-s", "KILL", "5", "mv", "a", "b"], frozenset())
        'mv'
    """
    return _words_unlisted(args[_skip_options(args, _TIMEOUT_VALUE_OPTIONS) + 1 :], known)


def _env_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge the command ``env`` runs, after its options and ``NAME=value`` settings; alone it only prints.

    Examples:
        >>> _env_use(["FOO=1", "grep", "x"], frozenset()) is None
        True
        >>> _env_use(["-u", "HOME", "mv", "a", "b"], frozenset())
        'mv'
        >>> _env_use([], frozenset()) is None
        True
    """
    index = _skip_options(args, _ENV_VALUE_OPTIONS)
    while index < len(args) and _ASSIGNMENT.match(args[index]):
        index = _assignment_end(args, index)
    return _words_unlisted(args[index:], known)


def _command_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge ``command``: ``-v``/``-V`` only look a name up; otherwise the named command runs.

    Examples:
        >>> _command_use(["-v", "jq"], frozenset()) is None
        True
        >>> _command_use(["mv", "a", "b"], frozenset())
        'mv'
        >>> _command_use(["mv", "a", "-v"], frozenset())
        'mv'
    """
    for arg in args:
        if arg == "--" or not arg.startswith("-"):
            break
        if re.match("-[A-Za-z]*[vV]", arg):
            return None
    return _words_unlisted(args[_skip_options(args, frozenset()) :], known)


def _nice_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge the command ``nice`` runs.

    Examples:
        >>> _nice_use(["-n", "10", "mv", "a", "b"], frozenset())
        'mv'
    """
    return _words_unlisted(args[_skip_options(args, frozenset({"-n"})) :], known)


def _plain_wrapper_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge the command ``nohup`` or ``time`` runs.

    Examples:
        >>> _plain_wrapper_use(["-p", "ls"], frozenset()) is None
        True
    """
    return _words_unlisted(args[_skip_options(args, frozenset()) :], known)


def _run_wrapper_rest(args: list[str], values: frozenset[str], flags: frozenset[str]) -> tuple[list[str], str | None]:
    """Split ``<tool> run`` arguments into the wrapped command and the first option the check does not know.

    An option outside ``values`` and ``flags`` may take a value of its own (``-C ruff``), so the word after it cannot
    be told from the wrapped command; the caller reports the option instead of guessing.

    Examples:
        >>> _run_wrapper_rest(["--frozen", "pytest"], frozenset(), frozenset({"--frozen"}))
        (['pytest'], None)
        >>> _run_wrapper_rest(["-C", "ruff", "rm"], frozenset(), frozenset())
        (['-C', 'ruff', 'rm'], '-C')
        >>> _run_wrapper_rest(["--python=3.12", "pytest"], frozenset({"--python"}), frozenset())
        (['pytest'], None)
    """
    index = 0
    while index < len(args) and args[index].startswith("-") and args[index] != "--":
        option = args[index].split("=", 1)[0]
        if option in values:
            index += 1 if "=" in args[index] else 2
        elif option in flags:
            index += 1
        else:
            return args, args[index]
    return args[index + 1 if index < len(args) and args[index] == "--" else index :], None


def _uv_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge ``uv``: a version query, or ``uv run`` of an allowlisted command; never installs or syncs.

    Examples:
        >>> _uv_use(["run", "pytest", "tests/"], frozenset()) is None
        True
        >>> _uv_use(["run", "--with", "x", "perl", "-e", "'1'"], frozenset())
        'perl'
        >>> _uv_use(["run", "-f", "ruff", "rm", "-rf", "x"], frozenset())
        'uv run -f'
        >>> _uv_use(["add", "--dev", "pkg"], frozenset())
        'uv add'
    """
    sub = args[0].strip("'\"") if args else ""
    if sub in _INFO_FLAGS or sub == "version":
        return None
    if sub != "run":
        return f"uv {sub}".rstrip()
    rest, unknown = _run_wrapper_rest(args[1:], _UV_RUN_VALUE_OPTIONS, _UV_RUN_FLAGS)
    return f"uv run {unknown}" if unknown else _words_unlisted(rest, known)


#: Scripts at a state path that a shell may run, by file name, each with the plugin code that writes its content. A
#: state file is otherwise data: its content comes from whatever step wrote it, so running it is never admitted.
_STATE_SCRIPTS = {
    "full-suite.sh": "cc_oss bin/resolve_test_plan.py full-command: one validated runner invocation, shlex-quoted",
}


def _shell_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge a shell interpreter: a ``-c`` string is judged as commands; a script operand must be the plugin's own.

    A script under ``$CLAUDE_PLUGIN_ROOT`` ships with the plugin. A script at a state path (:func:`_is_state_path`) is
    admitted only when its file name is in :data:`_STATE_SCRIPTS`, whose content the plugin's own code writes. Any
    other script operand is unlisted.

    Examples:
        >>> _shell_use(["-c", "'echo hi'"], frozenset()) is None
        True
        >>> _shell_use(['"$IMPL_DIR/full-suite.sh"'], frozenset({"$IMPL_DIR"})) is None
        True
        >>> _shell_use(['"$IMPL_DIR/steps.sh"'], frozenset({"$IMPL_DIR"}))
        'shell script $IMPL_DIR/steps.sh'
        >>> _shell_use(["build.sh"], frozenset())
        'shell script build.sh'
    """
    for index, raw in enumerate(args):
        token = raw.strip("'\"")
        if token in _INFO_FLAGS:
            return None
        if _has_short_flag([token], "c"):
            return _unlisted(_unquote_leading(" ".join(args[index + 1 :])), known)
        if not token.startswith("-"):
            state_script = _is_state_path(token, known) and PurePath(token).name in _STATE_SCRIPTS
            own = token.startswith(("${CLAUDE_PLUGIN_ROOT", "$CLAUDE_PLUGIN_ROOT/")) or state_script
            return None if own and ".." not in token else f"shell script {token}"
    return None


def _is_state_path(path: str, known: frozenset[str]) -> bool:
    """Report whether a path names the skill's own session state rather than an arbitrary file.

    State paths are a ``${TMPDIR:-/tmp}/…`` sentinel scoped by ``${CSID}``, a ``.temp/`` file, or a path under (or
    equal to) a variable the entry sets only from its own state or a literal (:func:`_trusted_variables`).

    Args:
        path: One path word with its quotes removed.
        known: Names known to the entry; ``$NAME`` marks a trusted variable.

    Returns:
        True for a state path without a ``..`` component.

    Examples:
        >>> _is_state_path("${TMPDIR:-/tmp}/oss-review-flags-${CSID}", frozenset())
        True
        >>> _is_state_path("$IMPL_DIR/pr-vars.sh", frozenset({"$IMPL_DIR"}))
        True
        >>> _is_state_path("$IMPL_DIR/pr-vars.sh", frozenset())
        False
        >>> _is_state_path("${TMPDIR:-/tmp}/shared-file", frozenset())
        False
        >>> _is_state_path(".temp/../x", frozenset())
        False
    """
    if ".." in path:
        return False
    if path.startswith("${TMPDIR:-/tmp}/") and ("${CSID}" in path or "$CSID" in path):
        return True
    if path.startswith(".temp/"):
        return True
    root = _VARIABLE_PREFIX.match(path)
    return root is not None and f"${root.group(1) or root.group(2)}" in known


def _cp_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge ``cp``: copying is allowed except onto or out of the filesystem root or the home directory itself.

    Examples:
        >>> _cp_use(["-p", '"$f"', '"$RUN_DIR/pre/$f"'], frozenset()) is None
        True
        >>> _cp_use(["-r", "build", "/"], frozenset())
        'cp /'
    """
    operands = [arg.strip("'\"") for arg in args if not arg.startswith("-")]
    return "cp /" if any(operand in _ROOT_PATHS for operand in operands) else None


def _source_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge ``.``/``source``: never admitted, because sourcing runs a file's content as shell code.

    A state file holds values an earlier step wrote, and those values can carry text from outside the skill (a PR's
    CI check names, a file path), so sourcing it turns data into commands. Read ``KEY=value`` lines with
    ``while IFS='=' read -r`` and a closed ``case`` of known keys instead.

    Examples:
        >>> _source_use(['"${TMPDIR:-/tmp}/oss-review-flags-${CSID}"'], frozenset())
        '. ${TMPDIR:-/tmp}/oss-review-flags-${CSID}'
        >>> _source_use(['"$HOME/.bashrc"'], frozenset())
        '. $HOME/.bashrc'
    """
    path = args[0].strip("'\"") if args else ""
    return f". {path}".rstrip()


def _unlink_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge ``unlink``: only a state path may be removed.

    Examples:
        >>> _unlink_use(['"${TMPDIR:-/tmp}/x-${CSID}"'], frozenset()) is None
        True
        >>> _unlink_use(['"$dest"'], frozenset())
        'unlink'
    """
    path = args[0].strip("'\"") if args else ""
    return None if _is_state_path(path, known) else "unlink"


def _kaggle_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge the ``kaggle`` CLI: only listing verbs that print and write nothing.

    Examples:
        >>> _kaggle_use(["competitions", "list", "-p", "1"], frozenset()) is None
        True
        >>> _kaggle_use(["competitions", "download", '"$SLUG"'], frozenset())
        'kaggle competitions download'
    """
    words = [arg.strip("'\"") for arg in args[:2]]
    if words[:1] and words[0] in _INFO_FLAGS:
        return None
    if len(words) == 2 and words[0] in _KAGGLE_GROUPS and words[1] in _KAGGLE_READ_VERBS:
        return None
    return " ".join(["kaggle", *words])


def _mv_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge ``mv``: a consume-marker rename, or a move between two of the skill's own state paths.

    The consume-marker shape (``mv "$F" "$F.done"``) can only ever replace the source's own sibling marker. A move whose
    source and target are both state paths (:func:`_is_state_path`), plain or forced with ``-f``, stays inside the
    skill's own session state: the atomic ``mv "$F.tmp" "$F"`` publish and parking a report beside itself. Every
    variable either operand names must be ``TMPDIR``, ``CSID`` or a trusted one: :func:`_is_state_path` accepts any
    ``.temp/`` path, and an untrusted value after that prefix (``.temp/$DEST`` with ``DEST=../../x``) would escape it
    at run time. Any other move can overwrite an arbitrary path and stays unlisted.

    Examples:
        >>> _mv_use(['"$_NOW"', '"$_NOW.done"'], frozenset()) is None
        True
        >>> _mv_use(['"$IMPL_DIR/c1.txt"', '"$IMPL_DIR/c1.txt.done"'], frozenset()) is None
        True
        >>> _mv_use(['"$CACHE_FILE.tmp"', '"$CACHE_FILE"'], frozenset({"$CACHE_FILE"})) is None
        True
        >>> _mv_use(["-f", '"$D/a.md"', '"$D/a.refused.md"'], frozenset({"$D"})) is None
        True
        >>> _mv_use(['"$D/a.md"', '"$HOME/a.md"'], frozenset({"$D"}))
        'mv'
        >>> _mv_use(['"$TMP"', '"$OUT"'], frozenset())
        'mv'
        >>> _mv_use(['".temp/a"', '".temp/$DEST"'], frozenset())
        'mv'
        >>> _mv_use(['"${TMPDIR:-/tmp}/x-${CSID}.tmp"', '".temp/x"'], frozenset()) is None
        True
        >>> _mv_use(["-f", "a", "a.done"], frozenset())
        'mv'
    """
    operands = args[1:] if args[:1] == ["-f"] else args
    if len(operands) != 2:
        return "mv"
    source, target = (arg.strip("'\"") for arg in operands)
    named = {f"${name}" for path in (source, target) for name in _VARIABLE_NAME.findall(path)}
    if named <= known | _STATE_PATH_VARIABLES and all(_is_state_path(path, known) for path in (source, target)):
        return None
    suffix = target.removeprefix(source) if source and target.startswith(source) else ""
    return None if operands is args and _MARKER_SUFFIX.fullmatch(suffix) else "mv"


def _trap_use(args: list[str], known: frozenset[str]) -> str | None:
    """Judge a ``trap`` handler as commands; resetting (``-``) or ignoring (``''``) a signal runs nothing.

    Examples:
        >>> _trap_use(["'rm", '-f', "x'", "EXIT"], frozenset())
        'rm'
        >>> _trap_use(["-", "EXIT"], frozenset()) is None
        True
        >>> _trap_use(["cleanup", "EXIT"], frozenset({"cleanup"})) is None
        True
    """
    if not args or args[0].strip("'\"") in ("", "-"):
        return None
    return _unlisted(_unquote_leading(" ".join(args)), known)


#: Command-specific judgements for command words outside :data:`SAFE_COMMANDS`, fed the raw words after the
#: command word. Each returns the offending use, or None when this use is allowed.
_ALLOWLIST_CHECKS: dict[str, Callable[[list[str], frozenset[str]], str | None]] = {
    "python": _python_use,
    "python3": _python_use,
    "node": _node_use,
    "awk": _awk_use,
    "curl": _curl_use,
    "find": _find_use,
    "xargs": _xargs_use,
    "timeout": _timeout_use,
    "env": _env_use,
    "command": _command_use,
    "nice": _nice_use,
    "nohup": _plain_wrapper_use,
    "time": _plain_wrapper_use,
    "uv": _uv_use,
    **dict.fromkeys(SHELL_INTERPRETERS, _shell_use),
    "trap": _trap_use,
    "mv": _mv_use,
    "cp": _cp_use,
    ".": _source_use,
    "source": _source_use,
    "unlink": _unlink_use,
    "kaggle": _kaggle_use,
}


def _words_unlisted(words: list[str], known: frozenset[str]) -> str | None:
    """Judge one command, given its raw words from the command word on.

    Args:
        words: Raw words, as returned by :func:`_command_words`.
        known: Names known to the entry: calling a function it defines is allowed, its body is judged where defined.
            A ``$NAME`` entry (:func:`_trusted_variables`) marks a trusted path root only; it never admits that
            variable as a command word, since a value read from state is data whose text the entry cannot see.

    Returns:
        The offending command word or use, or None when the command is allowed.

    Examples:
        >>> _words_unlisted(["grep", "-n", "x", "f"], frozenset()) is None
        True
        >>> _words_unlisted(["$RUNNER", "pytest", "-q"], frozenset({"$RUNNER"}))
        '$RUNNER'
        >>> _words_unlisted(["$CMD", "x"], frozenset())
        '$CMD'
        >>> _words_unlisted(["mv", "a", "b"], frozenset())
        'mv'
        >>> _words_unlisted(["f()", "{", "mv", "a", "b"], frozenset())
        'mv'
        >>> _words_unlisted([">", '"$OUT"'], frozenset()) is None
        True
        >>> _words_unlisted(["2>/dev/null", "mv", "a", "b"], frozenset())
        'mv'
        >>> _words_unlisted(["ARGS+=(--output", '"$F")'], frozenset()) is None
        True
    """
    words = _command_start(words)
    if not words:
        return None
    if _function_name(words) is not None:
        return _words_unlisted(_function_body(words), known)
    if _head_is_expansion(words):
        return words[0]
    head, _ = command_head(segment_tokens(" ".join(words)))
    if not head or head in SAFE_COMMANDS or head in known:
        return None
    check = _ALLOWLIST_CHECKS.get(head)
    return head if check is None else check(words[1:], known)


def _command_start(words: list[str]) -> list[str]:
    """Return the words from the real command word on, past keywords, assignments and leading redirections.

    :func:`_command_words` already skips keywords and ``NAME=value``; this also skips ``NAME+=value`` array appends
    and a redirection written before the command (``2>/dev/null mv a b`` runs ``mv``).

    Args:
        words: Raw words of one command.

    Returns:
        The words from the command word on; empty when only assignments or redirections remain.

    Examples:
        >>> _command_start(["then", "X+=(a", "b)", ">", "out", "echo", "hi"])
        ['echo', 'hi']
        >>> _command_start(["}", ">>", '"$F"'])
        []
    """
    words = _command_words(" ".join(words))
    while words:
        if _APPEND_ASSIGNMENT.match(words[0]):
            rest = words[_assignment_end(words, 0) :]
        elif _REDIRECTION_HEAD.match(words[0]):
            rest = words[2 if _BARE_REDIRECTION.fullmatch(words[0]) else 1 :]
        else:
            return words
        words = _command_words(" ".join(rest))
    return words


def _case_arm_after_in(text: str) -> list[str]:
    """Return the command words of a ``case`` arm written on the ``case … in`` line itself, if any.

    Examples:
        >>> _case_arm_after_in('case "$X" in a) mv x y')
        ['mv', 'x', 'y']
        >>> _case_arm_after_in('case "$X" in a')
        []
    """
    arm = _case_line_arm(text)
    return _command_words(arm) if arm is not None else []


def _segments_unlisted(segments: list[tuple[str, str]], known: frozenset[str]) -> str | None:
    """Judge top-level segments in order, reading ``case`` labels and pattern alternatives as syntax.

    Args:
        segments: Top-level segments, as returned by :func:`_top_level_segments`.
        known: Names known to the entry.

    Returns:
        The first offending command word or use, or None.
    """
    case_depth = 0
    for index, (segment, _separator) in enumerate(segments):
        text = segment
        if case_depth and _case_label_end(text) is None and _is_case_alternative(segments, index):
            continue
        if case_depth or _command_words(text)[:1] == ["case"]:
            # every label comes off, and each `case` opened on the way is one more level its `esac` closes
            chain = _case_arm_chain(text)
            case_depth += sum(_command_words(step)[:1] == ["case"] for step in chain[:-1])
            text = chain[-1]
        words = _command_words(text)
        if words[:1] == ["case"]:
            case_depth += 1
            words = _case_arm_after_in(text)
        elif words[:1] == ["esac"]:
            case_depth = max(case_depth - 1, 0)
        # the arm's command may itself hide commands (a zsh brace or short compound form, a function body), read
        # by the same reader the danger check uses
        hidden = [_command_words(extra) for extra in _judged_texts(text) if extra != text]
        for command in (words, *hidden):
            offender = _words_unlisted(command, known)
            if offender is not None:
                return offender
    return None


def _mask_substitutions(text: str) -> tuple[str, list[str]]:
    """Replace every substitution with an empty ``$()`` and return the bodies it held.

    The bodies are judged on their own; masking them keeps a ``case`` label, quote or parenthesis inside one from
    shifting the word boundaries of the command around it (``X=$(… case $a in b) …;; esac …)`` stays one
    assignment). Substitutions are found where bash expands them: top level and inside double quotes, never inside
    single quotes.

    Args:
        text: A command or block.

    Returns:
        Tuple of (text with each ``$(...)``, backtick and ``<(...)``/``>(...)`` body emptied, the bodies in order).

    Examples:
        >>> _mask_substitutions('X=$(case $a in b) echo;; esac) && echo "$(date)" \\'$(no)\\'')
        ('X=$() && echo "$()" \\'$(no)\\'', ['case $a in b) echo;; esac', 'date'])
    """
    pieces: list[str] = []
    bodies: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        read = None
        if quote != "'":
            read = _read_gate_substitution(text, index)
            if read is None and quote is None:
                read = _read_process_substitution(text, index)
        if read is not None:
            bodies.append(read[0])
            pieces.append("$()" if char in "$`" else f"{char}()")
            index = read[1]
            continue
        step = 2 if char == "\\" and quote != "'" else 1
        if quote is None and char in "'\"":
            quote = char
        elif quote is not None and char == quote:
            quote = None
        pieces.append(text[index : index + step])
        index += step
    return "".join(pieces), bodies


def _unlisted(command: str, known: frozenset[str]) -> str | None:
    """Judge every command in ``command``, substitution bodies included, against the allowlist.

    Heredoc bodies are data and are not judged as commands, except substitutions an unquoted delimiter leaves live.
    A heredoc that never reaches its delimiter cannot be split soundly and excludes the entry.

    Args:
        command: A command, a block, or text a ``trap``/``sh -c`` runs.
        known: Names already known from the enclosing text.

    Returns:
        The first offending command word or use, or None.
    """
    command, live, unterminated = _split_heredocs(command)
    if unterminated:
        return "unterminated heredoc"
    # a backslash-newline continues the command; left in, the backslash reads as a command word
    masked, bodies = _mask_substitutions(command.replace("\\\n", " "))
    segments = _top_level_segments(masked)
    defined = {_function_name(_command_words(text)) for text, _ in segments}
    known = known | {name for name in defined if name}
    offender = _segments_unlisted(segments, known)
    if offender is not None:
        return offender
    for inner in [*bodies, *live]:
        for body in _scan_top_level(inner, _read_gate_substitution)[1] if _is_arithmetic(inner) else [inner]:
            offender = _unlisted(body, known)
            if offender is not None:
                return offender
    return None


def _value_is_trusted(value: str, known: frozenset[str]) -> bool:
    """Report whether an assignment value is fixed text, a fresh ``mktemp`` path, or a state path.

    Examples:
        >>> _value_is_trusted('"python -m"', frozenset())
        True
        >>> _value_is_trusted("$(mktemp)", frozenset())
        True
        >>> _value_is_trusted('"${TMPDIR:-/tmp}/x-${CSID}"', frozenset())
        True
        >>> _value_is_trusted('"$(cat f)"', frozenset())
        False
    """
    if value.startswith("'"):
        return True
    bare = value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value
    if "$" not in bare and "`" not in bare:
        return True
    return _MKTEMP_CALL.fullmatch(bare) is not None or _is_state_path(bare, known)


def _trusted_variables(text: str) -> frozenset[str]:
    """Return the variables an entry sets only from its own state or from fixed text.

    A variable is trusted when every place the entry sets it is a ``read`` from a state path
    (:func:`_is_state_path`), a literal assignment, a ``$(mktemp)`` path, or a state-path assignment. Any other
    setting — a ``read`` from elsewhere, a ``for`` loop, an assignment from a substitution — distrusts it. Trust
    builds to a fixpoint, so ``IMPL_DIR`` read from a sentinel makes a later ``read … < "$IMPL_DIR/x"`` trusted too.

    Args:
        text: A whole normalized entry.

    Returns:
        Trusted variable names, without the ``$``.

    Examples:
        >>> sorted(_trusted_variables(
        ...     'IFS= read -r RUNNER < "${TMPDIR:-/tmp}/r-${CSID}" 2>/dev/null || RUNNER="python -m"'
        ... ))
        ['IFS', 'RUNNER']
        >>> sorted(_trusted_variables('X="a"\\nX=$(cat "$F")'))
        []
        >>> sorted(_trusted_variables('IFS= read -r D < "${TMPDIR:-/tmp}/d-${CSID}"\\nIFS= read -r P < "$D/p"'))
        ['D', 'IFS', 'P']
        >>> sorted(_trusted_variables("while IFS= read -r line; do echo; done < f"))
        ['IFS']
    """
    reads: list[tuple[list[str], str]] = []
    for match in _READ_TARGETS.finditer(text):
        source = _READ_SOURCE.match(match.group(2))
        reads.append((match.group(1).split(), source.group(2) if source else ""))
    assignments = _ASSIGNMENT_WORD.findall(text)
    loop_variables = set(_FOR_VARIABLE.findall(text))
    trusted: set[str] = set()
    for _ in range(len(reads) + len(assignments) + 1):
        known = frozenset(f"${name}" for name in trusted)
        good: set[str] = set()
        bad = set(loop_variables)
        for names, source_path in reads:
            (good if source_path and _is_state_path(source_path, known) else bad).update(names)
        for name, value in assignments:
            (good if _value_is_trusted(value, known) else bad).add(name)
        if good - bad == trusted:
            break
        trusted = good - bad
    return frozenset(trusted)


def unlisted_command(command: str) -> str | None:
    """Return the first command in ``command`` that the allowlist does not admit, or None.

    Args:
        command: A normalized block or logical command.

    Returns:
        The offending command word or use (``mv``, ``python -c``, ``$RUNNER``), or None when every command,
        including those inside substitutions, ``sh -c`` strings and ``find -exec``, is allowed.

    Examples:
        >>> unlisted_command('V=$(python "$ROOT/bin/x.py") && echo "$V"') is None
        True
        >>> unlisted_command("echo a && ln -sf a b")
        'ln'
        >>> unlisted_command('case "$M" in\\nall|grouped) echo ok ;;\\n*) exit 1 ;;\\nesac') is None
        True
        >>> unlisted_command("case x in a) mv p q;; esac")
        'mv'
        >>> unlisted_command('bash -c "$RUNNER pytest -q"')
        '$RUNNER'
        >>> unlisted_command('IFS= read -r RUNNER < "${TMPDIR:-/tmp}/r-${CSID}" || RUNNER="python -m"\\n'
        ...                  'bash -c "$RUNNER pytest -q"')
        '$RUNNER'
    """
    return _unlisted(command, frozenset(f"${name}" for name in _trusted_variables(command)))


def is_excluded(command: str) -> bool:
    """Report whether an entry stays out of the manifest: dangerous, or running a command the allowlist omits.

    Args:
        command: A normalized block or logical command.

    Returns:
        True when the entry must not be auto-allowed.

    Examples:
        >>> is_excluded("git status --short")
        False
        >>> is_excluded("git push origin main")
        True
        >>> is_excluded("mv a b")
        True
    """
    return is_dangerous(command) or unlisted_command(command) is not None


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

    A block holding a data heredoc gets no entry (:func:`has_data_heredoc`). A line becomes an entry of its own only
    when it is a complete command (:func:`is_complete_command`); an incomplete line is withheld without counting as
    dropped.

    Returns:
        Tuple of (digest → record mapping, number of entries dropped as dangerous, unlisted or a data-heredoc block).

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
        >>> block_entries("cat <<EOF\\nhi\\nEOF", "f.md:1")
        ({}, 1)
        >>> entries, dropped = block_entries("echo a\\nmv a b", "f.md:1")
        >>> [record["kind"] for record in entries.values()], dropped
        (['command'], 2)
        >>> entries, dropped = block_entries("if true; then\\n  echo a\\nfi", "f.md:1")
        >>> [record["kind"] for record in entries.values()], dropped
        (['block', 'command'], 0)
    """
    entries: dict[str, dict[str, str]] = {}
    dropped = 0
    if has_data_heredoc(normalized):
        return entries, 1
    if is_excluded(normalized):
        dropped += 1
    else:
        _record(entries, normalized, src, "block")
    if needs_bailout(normalized):
        return entries, dropped
    commands = split_logical_commands(normalized)
    if len(commands) < 2:
        return entries, dropped
    for command in commands:
        if is_excluded(command):
            dropped += 1
        elif is_complete_command(command):
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
            if aliases_escaped_blank(block.content):
                dropped += 1
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
