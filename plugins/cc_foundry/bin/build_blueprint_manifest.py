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
line, or a function definition puts behind a non-command word (:func:`_hidden_commands`), so
``case x in a) rm -rf /;; esac`` and ``f() { rm -rf x; }`` are dangerous; the JS twin mirrors
this and the shared vector fixture pins both.

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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePath

sys.path.insert(0, str(Path(__file__).resolve().parent))

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

#: Commands that are destructive enough that no auto-allow is ever warranted.
DANGER_COMMANDS: frozenset[str] = frozenset({"rm", "dd", "chmod", "chown", "mkfs", "shutdown", "kill", "pkill"})
#: Commands that carry another command in their arguments, so the destructive token is
#: never in head position — ``sudo rm -rf /``, ``find … | xargs rm -rf`` and
#: ``trap 'rm -f "$t"' EXIT`` all leak past a head-token-only check. ``noglob``, ``nocorrect``
#: and ``-`` are zsh precommand modifiers: ``noglob rm -rf x`` runs ``rm``.
DEFERRING_COMMANDS: frozenset[str] = frozenset(
    {
        "noglob",
        "nocorrect",
        "-",
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
        "uv",
        "poetry",
        "bash",
        "sh",
        "zsh",
        "dash",
        "ksh",
    }
)
#: Shell interpreters; one that reads its script from stdin runs whatever text is piped in.
SHELL_INTERPRETERS: frozenset[str] = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
#: Shell reserved words that precede a command word — ``then rm x`` runs ``rm``, not ``then``.
SHELL_RESERVED_WORDS: frozenset[str] = frozenset({"if", "then", "elif", "else", "while", "until", "do", "!"})
#: git subcommands that mutate history, a remote, or the working tree.
GIT_DANGER_TOKENS: frozenset[str] = frozenset({"push", "commit", "reset", "revert", "restore", "clean"})
#: Force-push style flags that mark a git command as dangerous when present.
GIT_FORCE_TOKENS: frozenset[str] = frozenset({"--force", "--force-with-lease", "-f"})
#: Flags that delete a branch when ``git branch`` carries them.
GIT_BRANCH_DELETE_TOKENS: frozenset[str] = frozenset({"-D", "-d", "--delete"})

#: Matches a leading ``NAME=value`` environment assignment that precedes the real command word.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: Characters that split a shell line into separate commands: ``;``, ``&``, ``|`` and newline.
_SEGMENT_SEPARATORS = ";&|\n"

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


#: Operators the completeness check reads at top level, longest first so ``;;&`` wins over ``;;`` and ``;``.
_LEX_OPERATORS = (
    *("&>>", ";;&", "<<<", "<<-", ">>", "<<", "<&", ">&", "<>", ">|", "&>", "&&", "||", ";;", ";&", "|&"),
    *("|", "&", ";", "(", ")", "<", ">"),
)
#: A redirection operator, with an optional descriptor written against it (``2>``, ``{fd}>``); it needs a target word.
_LEX_REDIRECTION = re.compile(r"(?:[0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:&>>|<<<|<<-|>>|<<|<&|>&|<>|>\||&>|<|>)")
#: A descriptor written against a redirection operator, kept in one token with it.
_LEX_DESCRIPTOR = re.compile(r"[0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\}")
#: Operators that end one command and put the next word in command position.
_LEX_SEPARATORS = frozenset({";", "&", "&&", "||", "|", "|&", "\n"})
#: Operators a logical command cannot end with: the pipeline or list goes on in a later line.
_LEX_CONTINUATIONS = frozenset({"|", "&&", "||", "|&"})
#: Operators that end one ``case`` arm.
_LEX_CASE_ENDS = frozenset({";;", ";&", ";;&"})
#: Keywords that open a compound command, each with the keyword that closes it.
_COMPOUND_OPENERS = {"if": "fi", "while": "done", "until": "done", "for": "done", "select": "done", "{": "}"}
#: Keywords valid only inside an open compound, each with the closer that compound expects.
_COMPOUND_MIDDLES = {"then": "fi", "elif": "fi", "else": "fi", "do": "done"}
#: Keywords that close a compound command.
_COMPOUND_CLOSERS = frozenset({"fi", "done", "esac", "}"})


def _close_backtick(text: str, index: int) -> int | None:
    """Return the index just past the backtick substitution opening at ``index``, or None when it never closes.

    Examples:
        >>> _close_backtick("`ls` x", 0)
        4
        >>> _close_backtick("`ls", 0) is None
        True
    """
    position = index + 1
    while position < len(text):
        if text[position] == "\\":
            position += 2
            continue
        if text[position] == "`":
            return position + 1
        position += 1
    return None


def _close_quote(text: str, index: int) -> int | None:
    """Return the index just past the quoted string opening at ``index``, or None when it never closes.

    A ``'…'`` string ends at the next quote; a ``"…"`` or ``$'…'`` string honours backslash escapes, and a ``"…"``
    string reads the expansions bash performs inside it whole.

    Examples:
        >>> _close_quote("'a b' c", 0)
        5
        >>> _close_quote('"a $(b ")") c" d', 0)
        14
        >>> _close_quote("$'a\\\\'b' c", 1)
        7
        >>> _close_quote("'open", 0) is None
        True
    """
    if text[index] == "'" and not (index > 0 and text[index - 1] == "$"):
        end = text.find("'", index + 1)
        return None if end < 0 else end + 1
    quote = text[index]
    position: int | None = index + 1
    while position is not None and position < len(text):
        char = text[position]
        if char == "\\":
            position += 2
        elif char == quote:
            return position + 1
        elif quote == '"' and char in "$`":
            position = _close_expansion(text, position)
        else:
            position += 1
    return None


def _close_expansion(text: str, index: int) -> int | None:
    """Return the index just past the expansion opening at ``index``, or None when it never closes.

    Reads ``$(…)``, ``$((…))``, ``${…}``, ``<(…)``, ``>(…)`` and backticks whole, quotes and nested expansions inside
    included; any other ``$`` is one character.

    Examples:
        >>> _close_expansion("$(a $(b) c) x", 0)
        11
        >>> _close_expansion("${X:-$(y)} z", 0)
        10
        >>> _close_expansion("<(sort f) x", 0)
        9
        >>> _close_expansion("$(open", 0) is None
        True
        >>> _close_expansion("$x", 0)
        1
    """
    if text[index] == "`":
        return _close_backtick(text, index)
    if not text.startswith(("$(", "${", "<(", ">("), index):
        return index + 1
    opener = text[index + 1]
    closer = ")" if opener == "(" else "}"
    depth = 0
    position: int | None = index + 1
    while position is not None and position < len(text):
        char = text[position]
        if char == "\\":
            position += 2
        elif char in "'\"":
            position = _close_quote(text, position)
        elif char in "$`":
            position = _close_expansion(text, position)
        else:
            depth += (char == opener) - (char == closer)
            if depth == 0:
                return position + 1
            position += 1
    return None


def _lex_top_level(text: str) -> list[str] | None:
    """Split text into top-level words and operators, reading quotes and expansions whole.

    A backslash-newline is removed, as bash removes it; a newline is a token of its own; a descriptor written against
    a redirection stays one token with it (``2>&``).

    Args:
        text: One logical command.

    Returns:
        The tokens, or None when a quote or an expansion never closes.

    Examples:
        >>> _lex_top_level('a "b c"|d 2>&1')
        ['a', '"b c"', '|', 'd', '2>&', '1']
        >>> _lex_top_level("x=$(a; b) && cat <(ls)")
        ['x=$(a; b)', '&&', 'cat', '<(ls)']
        >>> _lex_top_level("echo 'open") is None
        True
    """
    tokens: list[str] = []
    word = ""
    position: int | None = 0
    while position is not None and position < len(text):
        char = text[position]
        if text.startswith("\\\n", position):
            position += 2
        elif char == "\\":
            word, position = word + text[position : position + 2], position + 2
        elif char in "'\"$`" or text.startswith(("<(", ">("), position):
            end = _close_quote(text, position) if char in "'\"" else _close_expansion(text, position)
            word, position = (word + text[position:end], end) if end is not None else (word, None)
        elif char in " \t\n":
            tokens.extend(item for item in (word, "\n" if char == "\n" else "") if item)
            word, position = "", position + 1
        else:
            operator = next((op for op in _LEX_OPERATORS if text.startswith(op, position)), None)
            if operator is None:
                word, position = word + char, position + 1
                continue
            glued = _LEX_REDIRECTION.fullmatch(operator) is not None and _LEX_DESCRIPTOR.fullmatch(word) is not None
            tokens.extend([word + operator] if glued else [item for item in (word, operator) if item])
            word, position = "", position + len(operator)
    if position is None:
        return None
    return [*tokens, word] if word else tokens


class _ReadMode(str, Enum):
    """Where :class:`_CompoundReader` stands in the command's grammar."""

    COMMAND = "command"
    CASE_WORD = "case-word"
    CASE_IN = "case-in"
    PATTERN = "pattern"
    TEST = "test"


@dataclass
class _CompoundReader:
    """Track the compound commands one logical command opens and closes, token by token.

    Attributes:
        stack: Closers still owed, innermost last (``fi``, ``done``, ``esac``, ``}``, ``)``).
        mode: Where the reader stands: commands, the word or ``in`` after ``case``, a ``case`` pattern, or ``[[ … ]]``.
        command_position: True when the next word would be a command word, where keywords count.
        previous: The token read last.
        naming: True right after ``function``, whose next word is the function name.
    """

    stack: list[str] = field(default_factory=list)
    mode: _ReadMode = _ReadMode.COMMAND
    command_position: bool = True
    previous: str = ""
    naming: bool = False

    def feed(self, word: str, following: str | None) -> bool:
        """Advance over one token; return False when the token cannot stand here."""
        handlers = {
            _ReadMode.TEST: self._feed_test,
            _ReadMode.CASE_WORD: self._feed_case_word,
            _ReadMode.CASE_IN: self._feed_case_in,
            _ReadMode.PATTERN: self._feed_pattern,
            _ReadMode.COMMAND: self._feed_command,
        }
        accepted = handlers[self.mode](word, following)
        self.previous = word
        return accepted

    def closed(self) -> bool:
        """Report whether every compound is closed and the command does not go on in a later line."""
        return not self.stack and self.mode is _ReadMode.COMMAND and self.previous not in _LEX_CONTINUATIONS

    def _owes(self, closer: str) -> bool:
        """Report whether the innermost open compound expects ``closer``."""
        return bool(self.stack) and self.stack[-1] == closer

    def _feed_test(self, word: str, following: str | None) -> bool:
        """Skip the operands of ``[[ … ]]``, where parentheses and ``<`` are test syntax."""
        if word == "]]":
            self.mode, self.command_position = _ReadMode.COMMAND, False
        return True

    def _feed_case_word(self, word: str, following: str | None) -> bool:
        """Take the word ``case`` tests."""
        self.mode = _ReadMode.CASE_IN
        return True

    def _feed_case_in(self, word: str, following: str | None) -> bool:
        """Require ``in`` after the ``case`` word."""
        if word == "\n":
            return True
        self.mode = _ReadMode.PATTERN
        return word == "in"

    def _feed_pattern(self, word: str, following: str | None) -> bool:
        """Read a ``case`` pattern up to its ``)``, or the ``esac`` that ends the ``case``."""
        if word == "esac":
            if not self._owes("esac"):
                return False
            self.stack.pop()
            self.mode, self.command_position = _ReadMode.COMMAND, False
        elif word == ")":
            self.mode, self.command_position = _ReadMode.COMMAND, True
        return True

    def _feed_command(self, word: str, following: str | None) -> bool:
        """Read one token where commands stand."""
        if _LEX_REDIRECTION.fullmatch(word):
            target = following or ""
            return target not in ("", "\n") and target not in _LEX_OPERATORS and not _LEX_REDIRECTION.fullmatch(target)
        if word in _LEX_CASE_ENDS:
            self.mode = _ReadMode.PATTERN
            return self._owes("esac")
        if word in _LEX_SEPARATORS:
            dangling = word in _LEX_CONTINUATIONS and self.previous in ("", *_LEX_CONTINUATIONS)
            self.command_position = True
            return not dangling
        if word in ("(", ")"):
            return self._feed_parenthesis(word)
        keyword = self._feed_keyword(word) if self.command_position and not self.naming else None
        if keyword is not None:
            return keyword
        self.command_position, self.naming = self.naming, False
        return True

    def _feed_parenthesis(self, word: str) -> bool:
        """Open or close a subshell, or the ``()`` of a function definition."""
        self.command_position = True
        if word == "(":
            self.stack.append(")")
            return True
        if self._owes(")"):
            self.stack.pop()
            return True
        return False

    def _feed_keyword(self, word: str) -> bool | None:
        """Read a reserved word in command position; None when ``word`` is an ordinary word."""
        if word == "[[":
            self.mode = _ReadMode.TEST
        elif word == "case":
            self.stack.append("esac")
            self.mode = _ReadMode.CASE_WORD
        elif word in _COMPOUND_OPENERS:
            self.stack.append(_COMPOUND_OPENERS[word])
            self.command_position = word not in ("for", "select")
        elif word in _COMPOUND_MIDDLES:
            return self._owes(_COMPOUND_MIDDLES[word])
        elif word in _COMPOUND_CLOSERS:
            closes = self._owes(word)
            if closes:
                self.stack.pop()
            self.command_position = False
            return closes
        elif word == "function":
            self.naming = True
        elif word not in ("!", "time"):
            return None
        return True


def is_complete_command(command: str) -> bool:
    """Report whether one logical command closes every compound it opens, with nothing left dangling.

    :func:`block_entries` records a line as its own entry only when it is complete. A line such as ``if …; then``,
    ``f() {``, ``;;`` or ``ls |`` means nothing alone; as an entry it would let lines from different blocks be joined
    into a compound no shipped block contains. The check fails closed: a reading it cannot settle counts as incomplete,
    which only withholds that line's entry.

    Args:
        command: One logical command, as :func:`split_logical_commands` returns it.

    Returns:
        True when the command is complete.

    Examples:
        >>> is_complete_command("if [ -f x ]; then echo y; fi")
        True
        >>> is_complete_command("if [ -f x ]; then")
        False
        >>> is_complete_command("    ;;")
        False
        >>> is_complete_command("f() { echo hi; }")
        True
        >>> is_complete_command("_approved() {")
        False
        >>> is_complete_command('case "$M" in a|b) echo ok ;; *) exit 1 ;; esac')
        True
        >>> is_complete_command("X=$(printf a | while read -r i; do")
        False
        >>> is_complete_command("gh pr checks <pr-number>")
        False
        >>> is_complete_command("diff <(jq -S . a) <(jq -S . b) | head -40")
        True
        >>> is_complete_command("ls |")
        False
    """
    tokens = _lex_top_level(command)
    if tokens is None:
        return False
    reader = _CompoundReader()
    for index, word in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if not reader.feed(word, following):
            return False
    return reader.closed()


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
        >>> _read_delimited("$(jq '(.a)|x' f) y", 2, "(", ")")
        ("jq '(.a)|x' f", 16)

    A ``(…)`` body is shell code, so its quoted strings are read whole: a parenthesis inside ``'…'``, ``"…"`` or
    ``$'…'`` neither opens nor closes the body.
    """
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if open_char == "(" and (char in "'\"" or _ansi_c_body(text, index) is not None):
            index = _skip_quoted(text, index)
            continue
        if open_char is not None and char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start:index], index + 1
        index += 1
    return text[start:], len(text)


def _ansi_c_body(text: str, index: int) -> int | None:
    """Return where the body of a ``$'…'`` string opening at ``index`` starts, or None when none opens there.

    The shell removes a backslash-newline before it reads quotes, so ``$`` and ``'`` split across a line
    continuation still open ``$'…'`` quoting.

    Examples:
        >>> _ansi_c_body("$'a'", 0)
        2
        >>> _ansi_c_body("$\\\\\\n'a'", 0)
        4
        >>> _ansi_c_body("$a", 0) is None
        True
    """
    if text[index] != "$":
        return None
    index += 1
    while text.startswith("\\\n", index):
        index += 2
    return index + 1 if text.startswith("'", index) else None


def _skip_quoted(text: str, index: int) -> int:
    """Return the index just past the ``'…'``, ``"…"`` or ``$'…'`` string opening at ``index``.

    Examples:
        >>> _skip_quoted("'a)' b", 0)
        4
        >>> _skip_quoted('"a $(b ")") c" d', 0)
        14
        >>> _skip_quoted("$'a\\\\'b' c", 0)
        7
    """
    if text[index] == "'":
        end = text.find("'", index + 1)
        return len(text) if end < 0 else end + 1
    body = _ansi_c_body(text, index)
    if body is not None:
        return _read_ansi_c(text, body)[1]
    return _read_double_quoted(text, index + 1)[1]


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


def _is_arithmetic(inner: str) -> bool:
    """Report whether a ``$(...)`` body is really a ``$(( ...

    ))`` arithmetic expansion.
        Bash reads ``$((`` as arithmetic only when the paren it opens closes at the very end
        of the body; ``$((echo a); echo b)`` falls back to a command substitution holding a
        subshell, so that form is not arithmetic and its commands must still be judged.

        Args:
            inner: Inner text of a ``$(...)`` substitution, as returned by :func:`_read_substitution`.

        Returns:
            True when the whole body is one parenthesised expression.

        Examples:
            >>> _is_arithmetic("( $(wc -c < f) / 3 )")
            True
            >>> _is_arithmetic("(rm x);(echo)")
            False
            >>> _is_arithmetic("(echo a); echo b")
            False
            >>> _is_arithmetic("(unclosed (x)")
            False
            >>> _is_arithmetic("rm -rf x")
            False
    """
    if not (inner.startswith("(") and inner.endswith(")")):
        return False
    body, end = _read_delimited(inner, 1, "(", ")")
    return end == len(inner) and body == inner[1:-1]


def _scan_top_level(
    text: str, read_substitution: Callable[[str, int], tuple[str, int] | None] | None = None
) -> tuple[list[int], list[str], bool]:
    """Locate unquoted segment separators and expanded substitution bodies.

    Command substitutions are read at top level and inside double quotes — bash expands
    them in both — while process substitutions are read at top level only. Nothing is
    read inside single quotes, where bash expands nothing. A ``$'…'`` string honours its
    backslash escapes, so ``$'\\''`` does not end at the escaped quote. The ``&`` of a
    redirection (``2>&1``, ``&>/dev/null``, ``<&3``) is not a separator: the word after it
    is a file descriptor or a target, never a command.

    Args:
        text: A command or block.
        read_substitution: Reader for ``$(...)`` and backtick substitutions; defaults to
            :func:`_read_substitution`, the reader ``hooks/blueprint-allow.js`` mirrors. The
            allowlist gate passes :func:`_read_gate_substitution` instead.

    Returns:
        Tuple of (indices of top-level separator characters, inner text of each
        substitution found, whether the text ends inside an unclosed quote or holds a
        ``$'…'`` string with an escape bash and zsh read differently).

    Examples:
        >>> _scan_top_level("a; b")
        ([1], [], False)
        >>> _scan_top_level("echo 'a; b'")
        ([], [], False)
        >>> _scan_top_level("V=$(rm -rf x)")
        ([], ['rm -rf x'], False)
        >>> _scan_top_level("echo `ls`")
        ([], ['ls'], False)
        >>> _scan_top_level('echo "$(rm -rf x)"')
        ([], ['rm -rf x'], False)
        >>> _scan_top_level("echo '$(rm -rf x)'")
        ([], [], False)
        >>> _scan_top_level("cat <(rm -rf x)")
        ([], ['rm -rf x'], False)
        >>> _scan_top_level("a 2>&1 && b")
        ([7, 8], [], False)
        >>> _scan_top_level("echo $'\\\\''; b")
        ([10], [], False)
        >>> _scan_top_level("echo 'a")
        ([], [], True)
    """
    read_substitution = read_substitution or _read_substitution
    separators: list[int] = []
    substitutions: list[str] = []
    quote: str | None = None
    unsettled = False
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\" and quote != "'":
                index += 2
                continue
            read = read_substitution(text, index) if quote == '"' else None
            if read is not None:
                substitutions.append(read[0])
                index = read[1]
                continue
            if char == quote[-1]:
                quote = None
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        body = _ansi_c_body(text, index)
        if char in "'\"" or body is not None:
            quote = char if body is None else "$'"
            unsettled = unsettled or (body is not None and "\x00" in _read_ansi_c(text, body)[0])
            index = index + 1 if body is None else body
            continue
        read = read_substitution(text, index) or _read_process_substitution(text, index)
        if read is not None:
            substitutions.append(read[0])
            index = read[1]
            continue
        if char in _SEGMENT_SEPARATORS and not _is_redirection_ampersand(text, index):
            separators.append(index)
        index += 1
    return separators, substitutions, quote is not None or unsettled


def _conditional_end(command: str, edges: list[int], index: int) -> int:
    """Find where the segment starting after ``edges[index]`` ends, keeping a ``[[ … ]]`` test whole.

    Inside ``[[ … ]]`` the ``&&`` and ``||`` operators join conditions rather than commands, so
    splitting there would leave ``$VAR =~ x ]]`` standing as if ``$VAR`` were a command.

    Args:
        command: Full command text.
        edges: ``-1``, every separator index, then ``len(command)``.
        index: Position in ``edges`` of the separator that opens this segment.

    Returns:
        Position in ``edges`` of the separator that closes the segment. A ``[[`` never closed by
        a ``]]`` word leaves the segment unmerged.

    Examples:
        >>> text = "[[ -n $A && $B =~ x ]] || echo no"
        >>> edges = [-1, *_scan_top_level(text)[0], len(text)]
        >>> _conditional_end(text, edges, 0)
        3
        >>> _conditional_end("echo [[ a && b", [-1, 10, 14], 0)
        1
        >>> _conditional_end("[[ a && rm x", [-1, 5, 12], 0)
        1
    """
    end = index + 1
    if _command_words(command[edges[index] + 1 : edges[end]])[:1] != ["[["]:
        return end
    for candidate in range(end, len(edges)):
        if "]]" in command[edges[index] + 1 : edges[candidate]].split():
            return candidate
    return end


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
    separators, substitutions, _ = _scan_top_level(command)
    edges = [-1, *separators, len(command)]
    segments: list[str] = []
    index = 0
    while index < len(edges) - 1:
        end = _conditional_end(command, edges, index)
        if command[edges[index] + 1 : edges[end]].strip():
            segments.append(command[edges[index] + 1 : edges[end]])
        index = end
    for inner in substitutions:
        bodies = _scan_top_level(inner)[1] if _is_arithmetic(inner) else [inner]
        for body in bodies:
            segments.extend(split_segments(body))
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
        >>> _git_is_dangerous(["git", "restore", "f"])
        True
        >>> _git_is_dangerous(["git", "clean", "-fd"])
        True
        >>> _git_is_dangerous(["git", "checkout", "--", "f"])
        True
        >>> _git_is_dangerous(["git", "checkout", "main"])
        False
        >>> _git_is_dangerous(["git", "branch", "-D", "topic"])
        True
        >>> _git_is_dangerous(["git", "branch", "--list"])
        False
        >>> _git_is_dangerous(["git", "stash", "drop"])
        True
    """
    present = set(tokens)
    if present & GIT_DANGER_TOKENS or present & GIT_FORCE_TOKENS:
        return True
    if "worktree" in present and "remove" in present:
        return True
    if "checkout" in present and present & {"--", "."}:
        return True
    if "branch" in present and present & GIT_BRANCH_DELETE_TOKENS:
        return True
    return "stash" in present and bool(present & {"drop", "clear"})


def _has_short_flag(tokens: list[str], letter: str) -> bool:
    """Report whether any token is a short-option cluster carrying ``letter``.

    Args:
        tokens: Tokens of one segment.
        letter: Single option letter to look for.

    Returns:
        True for ``-i``, ``-ni`` and ``-i.bak`` style tokens; long options never match.

    Examples:
        >>> _has_short_flag(["-ni", "x"], "i")
        True
        >>> _has_short_flag(["-i.bak"], "i")
        True
        >>> _has_short_flag(["--in-place", "-n", "-E"], "i")
        False
    """
    return any(re.match(f"-[A-Za-z]*{letter}", token) for token in tokens)


def _sed_edits_in_place(tokens: list[str]) -> bool:
    """Report whether a ``sed`` segment rewrites its input files in place.

    Args:
        tokens: Tokens of the segment, as returned by :func:`segment_tokens`.

    Returns:
        True when ``-i`` (also in a cluster or with a backup suffix) or ``--in-place`` is present.

    Examples:
        >>> _sed_edits_in_place(["sed", "-i", "s/a/b/", "f"])
        True
        >>> _sed_edits_in_place(["sed", "--in-place=.bak", "s/a/b/", "f"])
        True
        >>> _sed_edits_in_place(["sed", "-n", "-E", "s/a/b/p", "f"])
        False
    """
    return _has_short_flag(tokens, "i") or any(token.startswith("--in-place") for token in tokens)


def _gh_api_deletes(tokens: list[str]) -> bool:
    """Report whether a ``gh api`` segment issues an HTTP ``DELETE``.

    Args:
        tokens: Tokens of the segment, as returned by :func:`segment_tokens`.

    Returns:
        True when ``-X DELETE``, ``-XDELETE``, ``--method DELETE`` or ``--method=DELETE`` is present.

    Examples:
        >>> _gh_api_deletes(["gh", "api", "-X", "DELETE", "repos/o/r/git/refs/heads/x"])
        True
        >>> _gh_api_deletes(["gh", "api", "--method=delete", "repos/o/r/issues/1"])
        True
        >>> _gh_api_deletes(["gh", "api", "repos/o/r/issues/1"])
        False
        >>> _gh_api_deletes(["gh", "api", "-X", "GET", "repos/o/r"])
        False
    """
    if "api" not in tokens:
        return False
    methods: list[str] = []
    for index, token in enumerate(tokens):
        if token in ("-X", "--method"):
            methods.extend(tokens[index + 1 : index + 2])
        elif token.startswith("--method="):
            methods.append(token.removeprefix("--method="))
        elif token.startswith("-X"):
            methods.append(token[2:].lstrip("="))
    return any(method.upper() == "DELETE" for method in methods)


def _reads_script_from_stdin(arguments: list[str]) -> bool:
    """Report whether a shell invocation runs the script piped into it (``curl … | sh``).

    Args:
        arguments: Tokens following the shell word.

    Returns:
        True when no ``-c`` string is given and either no script operand is present or ``-s`` / ``-`` asks for stdin.

    Examples:
        >>> _reads_script_from_stdin([])
        True
        >>> _reads_script_from_stdin(["-s", "--", "arg"])
        True
        >>> _reads_script_from_stdin(["-ex"])
        True
        >>> _reads_script_from_stdin(["build.sh"])
        False
        >>> _reads_script_from_stdin(["-c", "echo hi"])
        False
    """
    if _has_short_flag(arguments, "c"):
        return False
    operands = [token for token in arguments if not token.startswith("-")]
    return not operands or _has_short_flag(arguments, "s") or "-" in arguments


def _assignment_end(words: list[str], start: int) -> int:
    """Find the word after a ``NAME=value`` assignment whose value may span several words.

    Args:
        words: Whitespace-split words of one segment.
        start: Index of the assignment word.

    Returns:
        Index of the first word after the value. An unterminated quote, ``(`` or ``{`` falls
        back to skipping the assignment word alone, so the words after it are still judged.

    Examples:
        >>> _assignment_end(["FOO=1", "rm"], 0)
        1
        >>> _assignment_end(['FOO="a', 'b"', "rm"], 0)
        2
        >>> _assignment_end(["V=$(a", "b)", "rm"], 0)
        2
        >>> _assignment_end(['FOO="a', "rm"], 0)
        1
    """
    quote = ""
    depth = 0
    for index in range(start, len(words)):
        chars = iter(words[index])
        for char in chars:
            if char == "\\":
                next(chars, None)
            elif quote:
                quote = "" if char == quote else quote
            elif char in "'\"`":
                quote = char
            elif char in "({":
                depth += 1
            elif char in ")}" and depth:
                depth -= 1
        if not quote and not depth:
            return index + 1
    return start + 1


def _command_words(segment: str) -> list[str]:
    """Return a segment's whitespace words starting at its command word.

    Grouping punctuation, shell reserved words (``then``, ``do``, ``if`` …) and leading
    ``NAME=value`` assignments are skipped, so ``then rm x``, ``FOO="a b" rm x`` and
    ``V=$(a b) rm x`` all begin at ``rm``.

    Args:
        segment: One unquoted segment.

    Returns:
        The raw words from the command word on; empty when the segment holds no command.

    Examples:
        >>> _command_words("then rm -rf x")
        ['rm', '-rf', 'x']
        >>> _command_words('FOO="a b" rm -rf x')
        ['rm', '-rf', 'x']
        >>> _command_words(" ( if ! git push )")
        ['git', 'push', ')']
        >>> _command_words("V=1")
        []
    """
    words = segment.split()
    index = 0
    while index < len(words):
        bare = words[index].strip("(){}")
        if not bare or bare in SHELL_RESERVED_WORDS:
            index += 1
        elif _ASSIGNMENT.match(bare):
            index = _assignment_end(words, index)
        else:
            break
    return words[index:]


def _head_is_expansion(words: list[str]) -> bool:
    """Report whether the command word is itself an expansion (``$CMD``, ``${CMD}``, ``$(...)``).

    Bash runs whatever the expansion yields, which no static filter can judge.

    Args:
        words: Raw words of one segment, as returned by :func:`_command_words`.

    Returns:
        True when the command word's final path component starts with ``$`` or a backtick.

    Examples:
        >>> _head_is_expansion(["$CMD", "arg"])
        True
        >>> _head_is_expansion(['"${CMD:-ls}"'])
        True
        >>> _head_is_expansion(["$(printf", "rm)", "-rf", "x"])
        True
        >>> _head_is_expansion(["$ROOT/bin/tool", "$ARG"])
        False
        >>> _head_is_expansion(["echo", "$X"])
        False
        >>> _head_is_expansion([])
        False
    """
    if not words:
        return False
    head = words[0].strip("(){}").strip("'\"")
    return head.rsplit("/", 1)[-1].startswith(("$", "`"))


@dataclass(frozen=True)
class _ShellWord:
    """One shell word of a segment: its text as written and the value the shell builds from it.

    Attributes:
        raw: The word as written, quotes and escapes included.
        value: The word after quote removal: backslash escapes, ``'…'``, ``$'…'`` and ``"…"`` resolved and joined;
            expansions (``$x``, ``$(…)``, backticks) are kept as written because their result is unknown.
        quoted: True when any quote, ``$'…'`` or backslash escape took part in building ``value``.
        pattern: True when an unquoted ``*``, ``?``, ``[`` or ``{`` can make the shell expand the word into others.
    """

    raw: str
    value: str
    quoted: bool
    pattern: bool


#: A redirection operator with its optional file-descriptor prefix: ``2>``, ``>>``, ``<<<``, ``&>``, ``>&``, ``<>``,
#: and the descriptor-allocating ``{fd}>`` form.
_REDIRECTION_OPERATOR = re.compile(r"(?:[0-9]+|&|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:<<<|<<-?|<>|<&|>&|>>|>\||<|>)")
#: A word read so far that opens an array assignment when ``(`` follows: ``ARGS=(`` or ``ARGS+=(``.
_ARRAY_ASSIGNMENT_OPEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
#: A leading assignment word, written unquoted: ``FOO=…``, ``FOO+=…``, ``a[1]=…``.
_LEADING_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
#: Single-letter escapes of ``$'…'`` quoting and the character each one stands for.
_ANSI_C_ESCAPES = {
    **{"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"},
    **{"\\": "\\", "'": "'", '"': '"', "?": "?"},
}
#: Digit pattern and base of the numeric escapes of ``$'…'`` quoting that bash and zsh decode alike.
_ANSI_C_NUMERIC = {"x": (r"[0-9A-Fa-f]{1,2}", 16)}


def _read_ansi_c(text: str, index: int) -> tuple[str, int]:
    """Read the body of a ``$'…'`` string from its first inner character, resolving its escapes.

    Only escapes bash and zsh decode alike are resolved: the letter escapes, ``\\?``, ``\\xHH`` and an octal byte up
    to ``\\377``. Any other escape (``\\m``, ``\\u…``, ``\\c…``, ``\\400``) reads as one shell and differently as the
    other, so it decodes to a NUL, as an octal or hex zero does; a NUL in any word makes the command dangerous.

    Examples:
        >>> _read_ansi_c("a'sh", 0)
        ('a', 2)
        >>> _read_ansi_c("\\\\x61\\\\163h' x", 0)
        ('ash', 10)
        >>> _read_ansi_c("co\\\\mmands'", 0)
        ('co\\x00mands', 10)
        >>> _read_ansi_c("\\\\400'", 0)
        ('\\x00', 5)
    """
    out: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "'":
            return "".join(out), index + 1
        if char != "\\":
            out.append(char)
            index += 1
            continue
        escape = text[index + 1 : index + 2]
        index += 2
        if escape in _ANSI_C_ESCAPES:
            out.append(_ANSI_C_ESCAPES[escape])
        elif escape in _ANSI_C_NUMERIC or (escape in "01234567" and escape):
            pattern, base = _ANSI_C_NUMERIC.get(escape, (r"[0-7]{0,2}", 8))
            digits = re.match(pattern, text[index:])
            number = (escape if base == 8 else "") + (digits.group() if digits else "")
            out.append(chr(int(number, base)) if number and int(number, base) <= 0o377 else "\x00")
            index += len(digits.group()) if digits else 0
        else:
            out.append("\x00")
            index += 1 if escape == "c" and index < len(text) else 0
    return "".join(out), len(text)


def _read_expansion(text: str, index: int) -> int | None:
    """Return the index just past an expansion opening at ``index``, or None when none opens there.

    Covers ``$(…)``, backticks, ``${…}`` and the process substitutions ``<(…)``/``>(…)``, each read whole so the
    spaces inside never split a word.

    Examples:
        >>> _read_expansion("${x:-a b} y", 0)
        9
        >>> _read_expansion("$(a b) y", 0)
        6
        >>> _read_expansion("$x y", 0) is None
        True
    """
    if text.startswith("${", index):
        return _read_delimited(text, index + 2, "{", "}")[1]
    read = _read_substitution(text, index) or _read_process_substitution(text, index)
    return None if read is None else read[1]


def _read_double_quoted(text: str, index: int) -> tuple[str, int]:
    """Read the body of a ``"…"`` string from its first inner character.

    A backslash escapes only ``$``, a backtick, ``"``, ``\\`` and a newline (which it removes); expansions are kept.

    Examples:
        >>> _read_double_quoted('a\\\\"b $(c "d") e" f', 0)
        ('a"b $(c "d") e', 16)
    """
    out: list[str] = []
    while index < len(text):
        char = text[index]
        if char == '"':
            return "".join(out), index + 1
        if char == "\\":
            escaped = text[index + 1 : index + 2]
            out.append(escaped if escaped in '$`"\\' else "" if escaped == "\n" else char + escaped)
            index += 2
            continue
        end = _read_expansion(text, index)
        out.append(text[index:end] if end is not None else char)
        index = end if end is not None else index + 1
    return "".join(out), len(text)


def _read_quoted(text: str, index: int) -> tuple[str, int] | None:
    """Read one quoted or escaped piece of a word, or return None when none opens at ``index``.

    Examples:
        >>> _read_quoted("'a b'c", 0)
        ('a b', 5)
        >>> _read_quoted("\\\\-v x", 0)
        ('-', 2)
        >>> _read_quoted("abc", 0) is None
        True
    """
    char = text[index]
    if char == "\\":
        escaped = text[index + 1 : index + 2]
        return ("" if escaped == "\n" else escaped), index + 2
    if char == "'":
        end = text.find("'", index + 1)
        end = len(text) if end < 0 else end
        return text[index + 1 : end], end + 1
    body = _ansi_c_body(text, index)
    if body is not None:
        return _read_ansi_c(text, body)
    if char == '"' or text.startswith('$"', index):
        return _read_double_quoted(text, index + (1 if char == '"' else 2))
    return None


def _read_word(text: str, index: int) -> tuple[_ShellWord, int]:
    """Read one shell word starting at ``index``; return it and the index just past it.

    The word ends at unquoted whitespace, at a redirection operator, or at an unquoted ``(``/``)``, which is a word of
    its own. ``NAME=(…)`` array assignments and expansions are read whole.

    Examples:
        >>> _read_word("h$'a'sh -p", 0)[0].value
        'hash'
        >>> _read_word("ARGS=(a b) x", 0)[0].value
        'ARGS=(a b)'
        >>> _read_word("hash>/dev/null", 0)[0].value
        'hash'
    """
    start = index
    out: list[str] = []
    quoted = pattern = False
    while index < len(text):
        char = text[index]
        if char.isspace() or (char in "<>" and text[index + 1 : index + 2] != "("):
            break
        if char == "(" and not quoted and _ARRAY_ASSIGNMENT_OPEN.fullmatch("".join(out)):
            end = _read_delimited(text, index + 1, "(", ")")[1]
            out.append(text[index:end])
            index = end
            continue
        if char in "()":
            if not out:
                out.append(char)
                index += 1
            break
        piece = _read_quoted(text, index)
        if piece is not None:
            out.append(piece[0])
            quoted, index = True, piece[1]
            continue
        end = _read_expansion(text, index)
        pattern = pattern or (end is None and char in "*?[{")
        out.append(text[index : end if end is not None else index + 1])
        index = end if end is not None else index + 1
    return _ShellWord(text[start:index], "".join(out), quoted, pattern), index


def shell_words(segment: str) -> list[_ShellWord]:
    """Split one segment into the words the shell passes to its command, redirections left out.

    Quotes and escapes are resolved the way the shell joins them, so ``\\-v``, ``h$'a'sh`` and ``co""mmands`` read as
    ``-v``, ``hash`` and ``commands``. A redirection and its target (``2>/dev/null``, ``2> "a b"``, ``>&2``) are not
    arguments of the command and are dropped wherever they stand.

    Args:
        segment: One unquoted segment, as returned by :func:`split_segments`.

    Returns:
        The segment's words in order.

    Examples:
        >>> [word.value for word in shell_words('printf 2>/dev/null \\\\-v "$n" %s')]
        ['printf', '-v', '$n', '%s']
        >>> [word.value for word in shell_words('2>"a echo" set -A x')]
        ['set', '-A', 'x']
        >>> [word.value for word in shell_words("( rm -rf x )")]
        ['(', 'rm', '-rf', 'x', ')']
        >>> [word.value for word in shell_words("cat <(ls) >&2")]
        ['cat', '<(ls)']
    """
    words: list[_ShellWord] = []
    index = 0
    while index < len(segment):
        if segment[index].isspace() or segment.startswith("\\\n", index):
            index += 2 if segment[index] == "\\" else 1
            continue
        operator = _REDIRECTION_OPERATOR.match(segment, index)
        if operator and segment[operator.end() : operator.end() + 1] != "(":
            index = operator.end()
            while index < len(segment) and segment[index].isspace():
                index += 1
            if index < len(segment):
                index = _read_word(segment, index)[1]
            continue
        word, end = _read_word(segment, index)
        if end == index:
            word, end = _ShellWord(segment[index], segment[index], False, False), index + 1
        words.append(word)
        index = end
    return words


def _command_start_words(words: list[_ShellWord]) -> list[_ShellWord]:
    """Return the words from the command word on, past reserved words, grouping and leading assignments.

    Examples:
        >>> [word.value for word in _command_start_words(shell_words('then FOO="a b" ( rm x'))]
        ['rm', 'x']
    """
    index = 0
    while index < len(words):
        raw = words[index].raw
        if raw in SHELL_RESERVED_WORDS or not raw.strip("(){}") or _LEADING_ASSIGNMENT.match(raw):
            index += 1
        else:
            break
    return words[index:]


def _glob_class(body: str) -> str:
    """Translate the inside of a ``[…]`` pattern class into a regex class; an out-of-order range matches nothing.

    Examples:
        >>> _glob_class("!a-c")
        '[^a-c]'
        >>> _glob_class("Y-M")
        '(?!)'
    """
    negate = body[:1] in ("!", "^")
    chars = body[1:] if negate else body
    parts: list[str] = []
    index = 0
    while index < len(chars):
        if index + 2 < len(chars) and chars[index + 1] == "-":
            low, high = chars[index], chars[index + 2]
            if low <= high:
                parts.append(re.escape(low) + "-" + re.escape(high))
            index += 3
        else:
            parts.append(re.escape(chars[index]))
            index += 1
    if not parts:
        return "." if negate else "(?!)"
    return "[" + ("^" if negate else "") + "".join(parts) + "]"


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a shell filename pattern into a regex: ``*``, ``?`` and ``[…]``/``[!…]`` classes, the rest literal.

    Examples:
        >>> bool(_glob_regex("r?").fullmatch("rm"))
        True
        >>> bool(_glob_regex("h[!x]sh").fullmatch("hash"))
        True
        >>> bool(_glob_regex("[").fullmatch("["))
        True
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        close = pattern.find("]", index + 2) if char == "[" else -1
        if char == "*":
            out.append(".*")
        elif char == "?":
            out.append(".")
        elif close > 0:
            out.append(_glob_class(pattern[index + 1 : close]))
            index = close
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("".join(out))


#: A backslash escape or a single-quoted span (``'…'``, ``$'…'``), where ``$`` and a backtick expand nothing.
_INERT_SPAN = re.compile(r"\\.|\$?'[^']*'", re.DOTALL)


def _expansion_end(text: str, start: int) -> int:
    """Return the index just past the ``${…}``, ``$(…)`` or backtick expansion opening at ``start``.

    Nested brackets of the opening kind are counted, and an unterminated expansion runs to the end of the text.

    Examples:
        >>> _expansion_end("a${x:-${y}}b", 1)
        11
        >>> _expansion_end("`id`/x", 0)
        4
    """
    if text[start] == "`":
        end = text.find("`", start + 1)
        return len(text) if end == -1 else end + 1
    opening = text[start + 1]
    closing = "}" if opening == "{" else ")"
    depth = 0
    for index in range(start + 1, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


def _collapse_expansions(text: str) -> str:
    """Replace each ``${…}``, ``$(…)`` and backtick expansion with a lone ``$``.

    A ``/`` inside an expansion (``h${x:-/}sh``) is not a path separator, so the last path segment of a word is
    found only after its expansions are collapsed.

    Examples:
        >>> _collapse_expansions("h${x:-/}sh")
        'h$sh'
        >>> _collapse_expansions("$(git rev-parse --show-toplevel)/bin/x.py")
        '$/bin/x.py'
        >>> _collapse_expansions("$ROOT/bin/x")
        '$ROOT/bin/x'
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "`" or (text[index] == "$" and text[index + 1 : index + 2] in ("{", "(")):
            index = _expansion_end(text, index)
            out.append("$")
        else:
            out.append(text[index])
            index += 1
    return "".join(out)


def _last_segment(text: str) -> str:
    """Return the part of a word after its last path separator outside any expansion.

    Examples:
        >>> _last_segment("${ROOT}/bin/x.py")
        'x.py'
        >>> _last_segment("h${x:-/}sh")
        'h$sh'
    """
    return _collapse_expansions(text).rsplit("/", 1)[-1]


def _expands_mid_word(word: _ShellWord) -> bool:
    """Report whether a word's last path segment holds a live expansion after its first character.

    ``h${x}sh`` runs ``hash`` once ``x`` is set, so a wrapper carrying such a word may run a refused command. A word
    that starts with the expansion (``"$CMD"``) is judged by the allowlist instead, and a ``$`` inside single quotes
    or behind a backslash expands nothing.

    Examples:
        >>> _expands_mid_word(shell_words("h${x}sh")[0])
        True
        >>> _expands_mid_word(shell_words("h${x:-/}sh")[0])
        True
        >>> _expands_mid_word(shell_words('"$ROOT/bin/x.py"')[0])
        False
        >>> _expands_mid_word(shell_words("'a$b'")[0])
        False
        >>> _expands_mid_word(shell_words("'h'${x:-a}sh")[0])
        True

    A quoted or escaped span stands in as one literal character, so ``'h'${x}sh`` keeps its expansion mid-word.
    """
    name = _last_segment(_INERT_SPAN.sub("_", word.raw).replace('"', ""))
    return any(mark in name[1:] for mark in "$`")


#: A ``[…]`` pattern class with a quote or backslash inside it.
_QUOTED_CLASS = re.compile(r"\[[^\]]*[\\'\"]")


def _spelling_hides_name(word: _ShellWord) -> bool:
    """Report whether an unquoted pattern in command position can expand to a refused command name.

    ``r?``, ``/bin/r?`` and ``ha?h`` expand to ``rm`` or ``hash`` when a file of that name is present, so the name the
    shell looks up is not the text written. A brace expansion (``{ha,}sh``) builds new words outright. A pattern that
    can match no refused name (a ``case`` alternative such as ``tox*``, a ``[`` test) is left alone.

    Examples:
        >>> _spelling_hides_name(shell_words("/bin/r? x")[0])
        True
        >>> _spelling_hides_name(shell_words("{ha,}sh -p x")[0])
        True
        >>> _spelling_hides_name(shell_words("tox* x")[0])
        False
        >>> _spelling_hides_name(shell_words("[ -f x ]")[0])
        False
        >>> _spelling_hides_name(shell_words("h[\\\\!a]sh")[0])
        True

        >>> _spelling_hides_name(shell_words('item["verdict"]')[0])
        False

    A bracket class holding a quote or backslash (``h[\\!a]sh``) counts as any one character: quote removal turns its
    literal ``!`` or ``-`` into negation or a range, so the class read here is not the one the shell matches.
    """
    if not word.pattern:
        return False
    name = word.value.rsplit("/", 1)[-1]
    if ("{" in name and ("," in name or ".." in name)) or any(mark in name for mark in ("[:", "[=", "[.")):
        return True
    if _QUOTED_CLASS.search(word.raw):
        name = re.sub(r"\[[^\]]*\]", "?", name)
    matcher = _glob_regex(name)
    return any(matcher.fullmatch(refused) for refused in DANGER_COMMANDS | REBIND_COMMANDS)


def _bare_word(token: str) -> str:
    """Return a command word as the shell runs it: backslashes and quotes removed, directory dropped.

    ``\\rm``, ``r""m`` and ``'/bin/rm'`` all run ``rm``, so the danger checks compare this form, never the raw token.

    Examples:
        >>> _bare_word("\\\\rm")
        'rm'
        >>> _bare_word('r""m')
        'rm'
        >>> _bare_word("/bin/h'a'sh")
        'hash'
    """
    bare = token.replace("\\", "").replace("'", "").replace('"', "")
    return bare.rsplit("/", 1)[-1]


def _options(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Split the words after a segment's command word into its option words and its operands.

    An option can take its argument as the next word (``set -o noglob -A …``, ``read -t 5 -a …``, ``print -u 2 -v …``),
    so an option can follow a word that is not one. Every word before a ``--`` that starts with ``-`` or ``+`` therefore
    counts as an option, wherever it sits; reading one too many as an option can only refuse a block, never admit one.

    Examples:
        >>> _options(["read", "-r", "-A", "x", "-v"])
        (['-r', '-A', '-v'], ['x'])
        >>> _options(["set", "-o", "noglob", "-A", "x"])
        (['-o', '-A'], ['noglob', 'x'])
        >>> _options(["FOO=1", "set", "--", "-A"])
        ([], ['-A'])
    """
    _, rest = command_head(tokens)
    options: list[str] = []
    operands: list[str] = []
    for index, word in enumerate(rest):
        if word == "--":
            return options, operands + rest[index + 1 :]
        (options if word.startswith(("-", "+")) and len(word) > 1 else operands).append(word)
    return options, operands


def _set_assigns_array(tokens: list[str]) -> bool:
    """Report whether a ``set`` segment assigns an array (zsh ``set -A name …`` or ``set +A name …``).

    The array name can be any word or expansion (``set -Acommands …``, ``set -A $n …``), so no name check can vouch
    for it; any option word carrying ``A`` counts. The ``-e``, ``-u``, ``-x``, ``-o`` and ``-a`` options stay safe.

    Examples:
        >>> _set_assigns_array(["set", "-Acommands", "ruff", "/bin/rm"])
        True
        >>> _set_assigns_array(["set", "+A", "x", "y"])
        True
        >>> _set_assigns_array(["set", "-o", "noglob", "-A", "x", "y"])
        True
        >>> _set_assigns_array(["set", "-euo", "pipefail"])
        False
    """
    options, _ = _options(tokens)
    return any("A" in word for word in options)


def _read_assigns_by_name(tokens: list[str]) -> bool:
    """Report whether a ``read`` segment fills an array or a variable whose name comes from an expansion.

    ``read -a``/``read -A`` fill an array, and a name operand such as ``"$n"`` can name a command table, so both are
    treated as rebinds. Operands stop at the first redirection, so ``read -r v < "$f"`` stays safe.

    Examples:
        >>> _read_assigns_by_name(["read", "-A", "x"])
        True
        >>> _read_assigns_by_name(["read", "-r", "$n"])
        True
        >>> _read_assigns_by_name(["read", "-r", "-t", "5", "-a", "x"])
        True
        >>> _read_assigns_by_name(["IFS=", "read", "-r", "RUN_DIR"])
        False

    The tokens come from :func:`shell_words`, which has already dropped redirections, so ``read -r v < "$f"`` reaches
    this check as ``read -r v``.
    """
    options, operands = _options(tokens)
    if any("a" in word or "A" in word for word in options):
        return True
    return any("$" in word or "`" in word for word in operands)


def _print_assigns_variable(tokens: list[str]) -> bool:
    """Report whether a ``printf``/``print`` segment writes its output into a variable (``-v name``).

    Unlike ``set`` or ``read``, both stop reading options at their first other word, which for ``printf`` is the
    format: ``printf "%s\\n" --version`` prints ``--version``. Only the leading option words count, and an option of
    zsh ``print`` that takes a value (``-u 2``, ``-f fmt``, ``-C 3``, ``-x 4``, ``-X 4``) consumes the word after it.

    Examples:
        >>> _print_assigns_variable(["printf", "-v", "BASH_CMDS[x]", "%s", "/bin/rm"])
        True
        >>> _print_assigns_variable(["print", "-rv", "x", "y"])
        True
        >>> _print_assigns_variable(["print", "-u", "2", "-v", "$n", "x"])
        True
        >>> _print_assigns_variable(["printf", "%s -v", "x"])
        False
        >>> _print_assigns_variable(["printf", "%s\\n", "--version"])
        False
    """
    head, rest = command_head(tokens)
    value_letters = "ufCxX" if _bare_word(head) == "print" else ""
    index = 0
    while index < len(rest):
        word = rest[index]
        if word == "--" or not word.startswith("-") or len(word) < 2:
            return False
        if "v" in word:
            return True
        index += 2 if word[-1] in value_letters else 1
    return False


def _declares_by_name(tokens: list[str]) -> bool:
    """Report whether a declaring builtin makes a name reference or declares a name taken from an expansion.

    A name reference (``declare -n r=$n``, ``typeset -n``) writes through to whatever variable it names, and an
    operand such as ``"$n=/bin/rm"`` declares a variable named at run time; either can reach a command table.

    Examples:
        >>> _declares_by_name(["declare", "-n", "ref=BASH_CMDS"])
        True
        >>> _declares_by_name(["typeset", "$n=/bin/rm"])
        True
        >>> _declares_by_name(["local", "x=$1"])
        False
    """
    options, operands = _options(tokens)
    if any(word.startswith("-") and "n" in word for word in options):
        return True
    return any("$" in name or "`" in name for name, _, _ in (word.partition("=") for word in operands))


#: Builtins whose options name a variable or array to fill (``printf -v``, ``set -A``, ``read -a``, ``declare -n``,
#: ``unset -f``), so an option supplied by an expansion may rebind a command name.
_OPTION_REBINDERS = frozenset(
    {"set", "read", "printf", "print", "unset", "declare", "typeset", "local", "export", "readonly", "integer", "float"}
)


def _option_from_expansion(head: str, words: list[_ShellWord]) -> bool:
    """Report whether an expansion stands where a builtin still reads options, so its value may be one.

    ``printf "$o" "$n[x]" …`` with ``o=-v`` writes into a variable named at run time. ``printf`` and ``print`` read
    options only before their first other word; the others take an option anywhere before ``--``. An option word
    holding an expansion (``-$o``) counts too. ``print``'s value-taking options (``-u 2``) consume the next word.

    Examples:
        >>> _option_from_expansion("printf", shell_words('"$o" "$n[x]" %s y'))
        True
        >>> _option_from_expansion("printf", shell_words('"%s\\\\n" "$x"'))
        False
        >>> _option_from_expansion("set", shell_words('-e "$o" x'))
        True
        >>> _option_from_expansion("set", shell_words('-- "$@"'))
        False
        >>> _option_from_expansion("local", shell_words('x="$1"'))
        False
    """
    leading_only = head in ("printf", "print")
    index = 0
    while index < len(words):
        word = words[index]
        live = _INERT_SPAN.sub("_", word.raw)
        if word.value == "--":
            return False
        if live.lstrip('"')[:1] in ("$", "`"):
            return True
        if word.value.startswith(("-", "+")) and len(word.value) > 1:
            if "$" in live or "`" in live:
                return True
            index += 2 if head == "print" and word.value[-1] in "ufCxX" else 1
            continue
        if leading_only:
            return False
        index += 1
    return False


#: Commands that change what a command name runs (``hash``, ``alias``, ``enable``, ``disable``, ``functions``,
#: ``autoload``, ``zmodload``, ``emulate``), run a builtin behind a prefix the other checks would not see past
#: (``builtin``), fill a variable or array named on the command line (``mapfile``, ``readarray``, ``vared``,
#: ``zparseopts``, ``getln``), or run text built at run time (``eval``). Any of them can point a command name at
#: another program, and no shipped block runs one, so each is denied by name rather than judged by its arguments.
REBIND_COMMANDS: frozenset[str] = frozenset(
    {
        *("hash", "builtin", "enable", "disable", "alias", "unalias", "functions", "autoload", "zmodload"),
        *("emulate", "mapfile", "readarray", "vared", "zparseopts", "getln", "eval"),
    }
)
#: Any mention of a table that maps a name to what it runs: zsh ``commands``, ``functions``, ``aliases`` and their
#: ``galiases``/``saliases``/``dis_*`` siblings, ``fpath``/``FPATH``, and bash ``BASH_CMDS``/``BASH_ALIASES``. An
#: option prefix attached to the name (``set -Acommands``, ``printf -vBASH_CMDS[x]``) counts too. No shipped block
#: reads one either, so every mention is treated as a rebind instead of telling reads from writes.
_COMMAND_TABLE_USE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:[-+][A-Za-z]*)?"
    r"(?:commands|functions|aliases|galiases|saliases|dis_[a-z_]+|fpath|FPATH|BASH_CMDS|BASH_ALIASES)"
    r"(?![A-Za-z0-9_./-])"
)
#: Indirect parameter expansion, which can assign through a name held in another variable: zsh ``${(P)n::=…}`` and
#: bash ``${!n:=…}``.
_INDIRECT_EXPANSION = re.compile(r"\$\{(?:\([^)]*P|!)")


def _writes_command_table(command: str) -> bool:
    """Report whether a command may rebind a name through a command table or an indirect expansion.

    Examples:
        >>> _writes_command_table("commands=(ruff /bin/rm)")
        True
        >>> _writes_command_table("set -Acommands ruff /bin/rm")
        True
        >>> _writes_command_table("functions[ruff]='rm -rf x'")
        True
        >>> _writes_command_table(': ${(P)n::=/bin/rm}')
        True
        >>> _writes_command_table('echo "${commands[cat]}"')
        True
        >>> _writes_command_table("ls docs/commands.md")
        False
        >>> _writes_command_table('typeset "co""mmands[ruff]=/bin/rm"')
        True
        >>> _writes_command_table('echo "needs the real metric and guard commands."')
        False

    The table names are matched in the text as written and again with quotes and backslashes removed, because the
    shell joins ``co""mmands`` and ``\\commands`` into ``commands`` before it assigns anything.
    """
    unquoted = command.replace("\\", "").replace("'", "").replace('"', "")
    return any(_COMMAND_TABLE_USE.search(text) for text in (command, unquoted)) or bool(
        _INDIRECT_EXPANSION.search(command)
    )


def _unset_removes_function(tokens: list[str]) -> bool:
    """Report whether ``unset`` removes a function (``-f``, zsh ``-F``) or pattern-matched names (zsh ``-m``).

    Removing a function that shadows a command changes what a later command word runs, like ``unalias``.

    Examples:
        >>> _unset_removes_function(["unset", "-f", "ruff"])
        True
        >>> _unset_removes_function(["unset", "-v", "x"])
        False
        >>> _unset_removes_function(["unset", "x", "-f"])
        False
    """
    for word in tokens[1:]:
        if word == "--" or not word.startswith("-"):
            return False
        if re.match("-[A-Za-z]*[fFm]", word):
            return True
    return False


#: Command-specific destructive-use checks, keyed by command word and fed the segment's tokens.
_TOOL_CHECKS: dict[str, Callable[[list[str]], bool]] = {
    "unset": _unset_removes_function,
    "git": _git_is_dangerous,
    "sed": _sed_edits_in_place,
    "gh": _gh_api_deletes,
    "set": _set_assigns_array,
    "read": _read_assigns_by_name,
    "printf": _print_assigns_variable,
    "print": _print_assigns_variable,
    **dict.fromkeys(("declare", "typeset", "local", "export", "readonly", "integer", "float"), _declares_by_name),
}


def _deferred_is_dangerous(head: str, rest: list[str]) -> bool:
    """Judge the arguments of a command that merely carries another command.

    Args:
        head: A member of :data:`DEFERRING_COMMANDS`.
        rest: Tokens following ``head``.

    Returns:
        True when the arguments hold a destructive command or a destructive use of ``git``, ``sed`` or ``gh``.

    Examples:
        >>> _deferred_is_dangerous("xargs", ["rm", "-rf"])
        True
        >>> _deferred_is_dangerous("xargs", ["sed", "-i", "s/a/b/"])
        True
        >>> _deferred_is_dangerous("bash", [])
        True
        >>> _deferred_is_dangerous("timeout", ["5", "set", "-A", "x", "y"])
        True
        >>> _deferred_is_dangerous("timeout", ["5", "curl", "-s", "x"])
        False

    A command-specific check receives the words from the carried command word on, so ``timeout 5 set -A …`` is
    judged as ``set -A …`` rather than by the wrapper's own arguments.
    """
    if "-delete" in rest or (head in SHELL_INTERPRETERS and _reads_script_from_stdin(rest)):
        return True
    if any(_bare_word(token) in DANGER_COMMANDS | REBIND_COMMANDS for token in rest):
        return True
    for index, token in enumerate(rest):
        check = _TOOL_CHECKS.get(_bare_word(token))
        if check is not None and check(rest[index:]):
            return True
    return False


def _segment_is_dangerous(segment: str) -> bool:
    """Report whether one segment runs a destructive command.

    A segment is judged by its command token. When that token merely carries another
    command (``xargs``, ``sudo``, ``trap``, ``find``, ``noglob`` and friends — see
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
        >>> _segment_is_dangerous("eval echo hi")
        True
        >>> _segment_is_dangerous("noglob \\\\rm -rf x")
        True
        >>> _segment_is_dangerous("builtin -- hash -p /bin/rm ruff")
        True
        >>> _segment_is_dangerous("trap 'rm -f \\"$tmp\\"' EXIT")
        True
        >>> _segment_is_dangerous("find . -mtime +30 -delete")
        True
        >>> _segment_is_dangerous("find .reports -type d -mtime +30")
        False
        >>> _segment_is_dangerous("echo 'rm -rf x'")
        False
        >>> _segment_is_dangerous("then rm -rf x")
        True
        >>> _segment_is_dangerous('"$CMD" --version')
        True
        >>> _segment_is_dangerous("sed -i s/a/b/ f")
        True
        >>> _segment_is_dangerous("gh api -X DELETE repos/o/r/issues/1")
        True
        >>> _segment_is_dangerous("sh")
        True
        >>> _segment_is_dangerous("sh build.sh")
        False
        >>> _segment_is_dangerous("case x in a) rm -rf /")
        True
        >>> _segment_is_dangerous("b) rm -rf /")
        True
        >>> _segment_is_dangerous("f() { rm -rf x")
        True
    """
    hidden = _hidden_commands(segment)
    texts = hidden if _case_label_end(segment) is not None else [segment, *hidden]
    return any(_command_is_dangerous(text) for text in texts)


def _hidden_commands(segment: str) -> list[str]:
    """Return the commands a segment runs behind syntax that hides them from the command-word check.

    A ``case`` arm label (``b) rm x``), an arm written on the ``case … in`` line itself, and a function definition
    (``f() { rm x``) all put a non-command word first. Each hidden command is judged in addition to the segment as
    written, never instead of it, so reading one of these shapes can only add a dangerous verdict.

    Args:
        segment: One unquoted segment.

    Returns:
        The command text after the label, the ``case … in`` arm, or the function-definition prefix; empty when the
        segment has none.

    Examples:
        >>> _hidden_commands("b) rm -rf /")
        [' rm -rf /']
        >>> _hidden_commands("case x in a) rm -rf /")
        [' rm -rf /']
        >>> _hidden_commands("f() { rm -rf x")
        ['rm -rf x']
        >>> _hidden_commands("echo hi")
        []
    """
    hidden: list[str] = []
    label_end = _case_label_end(segment)
    if label_end is not None:
        hidden.append(segment[label_end:])
    arm = _case_line_arm(segment)
    if arm is not None:
        hidden.append(arm)
    words = _command_words(segment)
    if _function_name(words) is not None:
        hidden.append(" ".join(_function_body(words)))
    return hidden + _compound_bodies(segment)


#: Words opening a loop or condition whose zsh short or brace form (``for x (a) cmd``, ``if true { cmd }``,
#: ``repeat 3 cmd``) or bash ``coproc`` runs a command later on the same line.
_COMPOUND_HEADS = frozenset({"for", "foreach", "select", "repeat", "if", "elif", "while", "until", "coproc"})


def _compound_bodies(segment: str) -> list[str]:
    """Return the text after each word that may close a loop list or condition in a compound command.

    zsh runs ``for x (a) cmd``, ``select x (a) cmd``, ``while [[ … ]] cmd``, ``if true { cmd }`` and ``repeat 1 cmd``
    without a ``do`` or ``then``, and bash ``coproc cmd`` runs its command in the background, so the command sits
    after a ``)``, ``{``, ``}`` or ``]]``, after ``repeat``'s count, or right after ``coproc``. Every such suffix is
    judged as a command; judging a list word as one can only refuse a block.

    Examples:
        >>> _compound_bodies("for x (a) hash -p y z")
        ['hash -p y z']
        >>> _compound_bodies("if true { hash -p y z }")
        ['hash -p y z }', '']
        >>> _compound_bodies("repeat 1 hash")
        ['hash']
        >>> _compound_bodies("echo (a) hash")
        []
    """
    words = [word.raw for word in shell_words(segment)]
    if not words or words[0] not in _COMPOUND_HEADS:
        return []
    starts = {1} if words[0] == "coproc" else {2} if words[0] == "repeat" else set()
    starts.update(index + 1 for index, word in enumerate(words) if word in ("{", "}", "]]") or word.endswith(")"))
    return [" ".join(words[start:]) for start in sorted(starts) if start <= len(words)]


def _command_is_dangerous(segment: str) -> bool:
    """Judge one command by its command word, scanning the arguments of a deferring command too.

    The segment is read as shell words (:func:`shell_words`): quotes and escapes resolved, redirections dropped,
    so ``2>/dev/null set -A …`` is judged as ``set`` and ``printf \\-v …`` as ``printf -v``. A command word spelled
    so that its text is not the name looked up is refused (:func:`_spelling_hides_name`), a table name anywhere in
    the words is refused (:data:`_COMMAND_TABLE_USE`), and a shell's ``-c`` string is judged as a command of its own
    (:func:`_carried_commands`).

    Examples:
        >>> _command_is_dangerous(" rm -rf /")
        True
        >>> _command_is_dangerous("2>/dev/null set -A n x")
        True
        >>> _command_is_dangerous("zsh -o shwordsplit -c 'x=1;set -A $n y'")
        True
        >>> _command_is_dangerous("h$'a'sh -p /bin/rm ruff")
        True
        >>> _command_is_dangerous("typeset BASH_C$''MDS[ruff]=/bin/rm")
        True
        >>> _command_is_dangerous('bash -c "$METRIC_CMD"')
        False
        >>> _command_is_dangerous("echo hi")
        False
        >>> _command_is_dangerous("typeset BASH_C$'\\\\0'MDS[ruff]=/bin/echo")
        True
        >>> _command_is_dangerous("command h[a]sh -p /bin/echo ruff")
        True
        >>> _command_is_dangerous("{fd}>/dev/null set -A n x")
        True
        >>> _command_is_dangerous("x=a; h${x:-/}sh -p /bin/echo ruff")
        True
        >>> _command_is_dangerous("unset -f ruff")
        True

    A decoded NUL is refused outright: bash ends a ``$'…'`` string there and zsh keeps it, so no single reading of
    the word is right for both shells. A pattern among a deferring command's words is refused like one in command
    position, since that word may be the command the wrapper runs.
    """
    words = _command_start_words(shell_words(segment))
    if not words:
        return False
    values = [word.value for word in words]
    if any("\x00" in value for value in values):
        return True
    head = _bare_word(values[0])
    if head in DANGER_COMMANDS or head in REBIND_COMMANDS or _spelling_hides_name(words[0]):
        return True
    if any(mark in _last_segment(values[0]) for mark in "$`") or any(_COMMAND_TABLE_USE.search(v) for v in values):
        return True
    if head in _OPTION_REBINDERS and _option_from_expansion(head, words[1:]):
        return True
    check = _TOOL_CHECKS.get(head)
    if check is not None:
        return check(values)
    if head not in DEFERRING_COMMANDS:
        return False
    if any(_spelling_hides_name(word) or _expands_mid_word(word) for word in words[1:]):
        return True
    return _deferred_is_dangerous(head, values[1:]) or any(is_dangerous(text) for text in _carried_commands(values))


#: Long shell options whose value is the next word.
_SHELL_VALUE_OPTIONS = frozenset({"--rcfile", "--init-file"})


def _shell_script(args: list[str]) -> list[str]:
    """Return every argument that may be the ``-c`` script among a shell's arguments.

    Options are read as clusters: each ``o``/``O`` letter (``-o name``, ``-xo name``, bash ``-O shopt``) takes the
    next word as its value. Once a cluster carries ``c``, every later argument is returned, so a value the reader
    misplaces (``-co shwordsplit 'script'``) is still judged as a script, never skipped.

    Examples:
        >>> _shell_script(["-o", "shwordsplit", "-ec", "set -A x y"])
        ['set -A x y']
        >>> _shell_script(["build.sh", "-c", "x"])
        []
        >>> _shell_script(["-c", "--", "hash -p y"])
        ['hash -p y']
        >>> _shell_script(["-co", "shwordsplit", "hash y", "-v"])
        ['shwordsplit', 'hash y', '-v']
        >>> _shell_script(["-xo", "shwordsplit", "-c", "hash y"])
        ['hash y']
    """
    index = 0
    while index < len(args):
        option = args[index]
        if option == "--" or not option.startswith(("-", "+")) or len(option) < 2:
            return []
        if option.startswith("-") and _has_short_flag([option], "c"):
            return [arg for arg in args[index + 1 :] if arg != "--"]
        values = (
            1 if option in _SHELL_VALUE_OPTIONS else 0 if option.startswith("--") else len(re.findall("[oO]", option))
        )
        index += 1 + values
    return []


def _env_split_string(args: list[str]) -> list[str]:
    """Return every string ``env -S``/``--split-string`` may split into a command line.

    ``S`` is found anywhere in a short-option cluster (``-iS``); the rest of the cluster after it and the next word
    are both returned, since either may be its value.

    Examples:
        >>> _env_split_string(["-S", "rm -rf x"])
        ['rm -rf x']
        >>> _env_split_string(["--split-string=hash -p y"])
        ['hash -p y']
        >>> _env_split_string(["-iS", "hash -p y"])
        ['hash -p y']
        >>> _env_split_string(["-Shash -p y"])
        ['hash -p y']
    """
    for index, arg in enumerate(args):
        if arg == "--split-string":
            return args[index + 1 : index + 2]
        if arg.startswith("--split-string="):
            return [arg.split("=", 1)[1]]
        if re.match("-[A-Za-z]*S", arg):
            rest = arg[arg.index("S") + 1 :]
            return ([rest] if rest else []) + args[index + 1 : index + 2]
    return []


def _carried_commands(values: list[str]) -> list[str]:
    """Return the command lines a deferring command runs from a single argument.

    A shell's ``-c`` string (``zsh -o shwordsplit -c '…'``, also behind ``xargs`` or ``sudo``), a ``trap`` action,
    a ``watch`` command and an ``env -S`` string each arrive as one argument holding a whole command line, which is
    judged as a command of its own. A string that is one bare variable (``bash -c "$METRIC_CMD"``) is left out: its
    text is not in the block, so the allowlist gate decides whether that variable is trusted
    (:func:`_trusted_variables`). Other arguments are command words, already judged by name.

    Examples:
        >>> _carried_commands(["xargs", "zsh", "-o", "shwordsplit", "-c", "x=1;hash y"])
        ['x=1;hash y']
        >>> _carried_commands(["trap", 'rm -f "$tmp"', "EXIT"])
        ['rm -f "$tmp"', 'EXIT']
        >>> _carried_commands(["bash", "-c", "$METRIC_CMD"])
        []
        >>> _carried_commands(["xargs", "grep", "-n", "a|b"])
        []
    """
    texts: list[str] = []
    for index, value in enumerate(values):
        name = _bare_word(value)
        if name in SHELL_INTERPRETERS:
            texts.extend(_shell_script(values[index + 1 :]))
        elif name in ("trap", "watch"):
            texts.extend(arg for arg in values[index + 1 :] if not arg.startswith("-"))
        elif name == "env":
            texts.extend(_env_split_string(values[index + 1 :]))
    return [text for text in texts if not _VARIABLE_WORD.fullmatch(text)]


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
        >>> is_dangerous("eval echo hi")
        True
        >>> is_dangerous("set -Acommands ruff /bin/rm; ruff -rf x")
        True
        >>> is_dangerous("cat <(rm -rf /)")
        True
        >>> is_dangerous('echo "$(rm -rf /)"')
        True
        >>> is_dangerous("echo '$(rm -rf /)'")
        False
        >>> is_dangerous("echo 'a; rm -rf x")
        True

    Text that ends inside an unclosed quote, at top level or in a substitution body, is refused: the reader and the
    shell disagree about where its commands start, so no verdict on its segments can be trusted. So is a ``$'…'``
    string anywhere in it, assignment or redirection target included, holding an escape bash and zsh read differently
    (:func:`_read_ansi_c`).
    """
    if _writes_command_table(command) or _quoting_unsettled(command):
        return True
    return any(_segment_is_dangerous(segment) for segment in split_segments(command))


def _quoting_unsettled(command: str) -> bool:
    """Report whether ``command`` or a substitution body in it ends inside an unclosed quote or has an unsettled escape.

    Examples:
        >>> _quoting_unsettled("echo 'a' \\"$(echo 'b)\\"")
        True
        >>> _quoting_unsettled("echo $'\\\\'' x")
        False
    """
    _, substitutions, unclosed = _scan_top_level(command)
    return unclosed or any(_quoting_unsettled(inner) for inner in substitutions)


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
#: One ``case`` pattern: unquoted characters other than space, parentheses and ``|``, escapes, or quoted runs.
#: Whitespace is spelled out as ASCII, not ``\s``, so ``hooks/blueprint-allow.js`` can match it exactly.
_CASE_PATTERN = r"""(?:[^ \t\n\r\f\v()'"|\\]|\\.|"[^"]*"|'[^']*')+"""
#: A ``case`` arm label at the start of a segment: one pattern closed by ``)``, optionally opened by ``(``.
_CASE_LABEL = re.compile(rf"[ \t\n\r\f\v]*\(?{_CASE_PATTERN}\)")
#: A segment that is one bare ``case`` pattern, an alternative before ``|`` (``a`` in ``a|b)``).
_CASE_ALTERNATIVE = re.compile(rf"[ \t\n\r\f\v]*\(?{_CASE_PATTERN}[ \t\n\r\f\v]*")
#: The ``in`` keyword of a ``case`` line.
_CASE_IN = re.compile(r"[ \t]in(?:[ \t]|$)")
#: A shell function name as written in a definition.
_FUNCTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
#: A heredoc operator: ``<<`` or ``<<-``, then the delimiter, optionally quoted; never the ``<<<`` here-string.
_HEREDOC_OPERATOR = re.compile(r"<<(?!<)(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
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
#: A word that is exactly one plain variable expansion, ``$NAME`` or ``${NAME}``.
_VARIABLE_WORD = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
#: A leading ``$NAME`` or ``${NAME}``, alone or followed by ``/``: the root of a path built on a variable.
_VARIABLE_PREFIX = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))(?:/|$|\.[A-Za-z0-9_-]+$)")
#: Any ``$NAME`` or ``${NAME…}`` reference in a path word, capturing the name.
_VARIABLE_NAME = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
#: Variables a session state path may name without the entry having set them: the temp root and the session token.
_STATE_PATH_VARIABLES = frozenset({"$TMPDIR", "$CSID"})
#: ``case`` and ``esac`` keywords, counted to tell a pattern's ``)`` from the end of a ``$(...)`` body.
_CASE_KEYWORD = re.compile(r"(?<![\w$-])case(?=[ \t])")
_ESAC_KEYWORD = re.compile(r"(?<![\w$-])esac(?![\w-])")
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


def _case_label_end(text: str) -> int | None:
    """Return where a leading ``case`` arm pattern closed by ``)`` ends, or None when the text opens with none.

    Args:
        text: One segment.

    Returns:
        Index just past the label's ``)``; quoted and backslash-escaped spaces stay inside the pattern.

    Examples:
        >>> _case_label_end("  grouped) echo x")
        10
        >>> _case_label_end('"make test")')
        12
        >>> _case_label_end("*[!0-9\\\\ ]*) exit 1")
        11
        >>> _case_label_end("$(cmd) x") is None
        True
        >>> _case_label_end("rm -rf x") is None
        True
    """
    match = _CASE_LABEL.match(text)
    return match.end() if match else None


def _read_command_body(text: str, start: int) -> tuple[str, int]:
    """Read a ``$(...)`` body the way bash does, from the first character inside it.

    Unlike :func:`_read_delimited`, a parenthesis inside quotes and the ``)`` closing a ``case`` pattern do not
    end the body, so ``$(jq '"\\(.a)"')`` and ``$(while …; do case $x in a) …;; esac; done)`` are read whole.

    Args:
        text: Full text.
        start: Index just past the opening ``$(``.

    Returns:
        Tuple of (body text, index just past the closing parenthesis).

    Examples:
        >>> _read_command_body("$(jq -r '\\"\\\\(.a)\\"' f) x", 2)
        ('jq -r \\'"\\\\(.a)"\\' f', 20)
        >>> _read_command_body("$(case $x in a) echo;; esac) y", 2)
        ('case $x in a) echo;; esac', 28)
    """
    depth = 1
    quote: str | None = None
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote is not None:
            if quote == '"' and text.startswith("$(", index):
                index = _read_command_body(text, index + 2)[1]
                continue
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")" and not (depth == 1 and _inside_case(text[start:index])):
            depth -= 1
            if depth == 0:
                return text[start:index], index + 1
        index += 1
    return text[start:], len(text)


def _inside_case(body: str) -> bool:
    """Report whether a body read so far has opened more ``case`` statements than it closed.

    Examples:
        >>> _inside_case("case $x in a")
        True
        >>> _inside_case("case $x in a) b;; esac")
        False
    """
    return len(_CASE_KEYWORD.findall(body)) > len(_ESAC_KEYWORD.findall(body))


def _read_gate_substitution(text: str, index: int) -> tuple[str, int] | None:
    """Read a ``$(...)`` or backtick substitution for the allowlist gate, with :func:`_read_command_body`.

    Examples:
        >>> _read_gate_substitution("$(a 'b)' c) d", 0)
        ("a 'b)' c", 11)
        >>> _read_gate_substitution("echo", 0) is None
        True
    """
    if text.startswith("$(", index):
        return _read_command_body(text, index + 2)
    return _read_substitution(text, index)


def _is_redirection_ampersand(command: str, index: int) -> bool:
    """Report whether the separator at ``index`` is the ``&`` of ``>&``, ``<&`` or ``&>``, not a command boundary.

    Examples:
        >>> _is_redirection_ampersand("a 2>&1", 4)
        True
        >>> _is_redirection_ampersand("a &>/dev/null", 2)
        True
        >>> _is_redirection_ampersand("a && b", 2)
        False
    """
    if command[index] != "&":
        return False
    before = command[index - 1] if index > 0 else ""
    after = command[index + 1] if index + 1 < len(command) else ""
    return before in ("<", ">") or after == ">"


def _top_level_segments(command: str) -> list[tuple[str, str]]:
    """Return each top-level segment with the separator character that ends it.

    Same boundaries as :func:`split_segments`, without the substitution bodies, except that the ``&`` of a
    redirection (``2>&1``, ``&>/dev/null``, ``<&3``) does not split: the word after it is a file descriptor or a
    target, not a command. The separator tells a ``case`` pattern alternative (``a|b)``) from a pipeline stage.

    Args:
        command: A command or block.

    Returns:
        ``(segment, separator)`` pairs; the separator is ``""`` for the last segment.

    Examples:
        >>> _top_level_segments("a|b) echo x;; esac")
        [('a', '|'), ('b) echo x', ';'), (' esac', '')]
        >>> _top_level_segments("cmd >/dev/null 2>&1 && echo ok")
        [('cmd >/dev/null 2>&1 ', '&'), (' echo ok', '')]
    """
    separators = [
        index
        for index in _scan_top_level(command, _read_gate_substitution)[0]
        if not _is_redirection_ampersand(command, index)
    ]
    edges = [-1, *separators, len(command)]
    segments: list[tuple[str, str]] = []
    index = 0
    while index < len(edges) - 1:
        end = _conditional_end(command, edges, index)
        text = command[edges[index] + 1 : edges[end]]
        if text.strip():
            segments.append((text, command[edges[end]] if edges[end] < len(command) else ""))
        index = end
    return segments


def _is_case_alternative(segments: list[tuple[str, str]], index: int) -> bool:
    """Report whether a segment is a pattern alternative (``a`` in ``a|b)``) rather than a command.

    Args:
        segments: Top-level segments, as returned by :func:`_top_level_segments`.
        index: Position of the segment to judge.

    Returns:
        True when the segment is one pattern piped into further bare patterns that end at a ``case`` label.

    Examples:
        >>> _is_case_alternative([("a", "|"), ("b", "|"), ("c) echo", ";")], 0)
        True
        >>> _is_case_alternative([("tox*", "|"), ('"make test")', ";")], 0)
        True
        >>> _is_case_alternative([("a) cat f ", "|"), (" sort ", ";"), ("b) echo", ";")], 1)
        False
    """
    text, separator = segments[index]
    if separator != "|" or not _CASE_ALTERNATIVE.fullmatch(text):
        return False
    for following, following_separator in segments[index + 1 :]:
        if _case_label_end(following) is not None:
            return True
        if following_separator != "|" or not _CASE_ALTERNATIVE.fullmatch(following):
            return False
    return False


def _function_name(words: list[str]) -> str | None:
    """Return the name a segment defines as a shell function, or None.

    Args:
        words: Raw words of one segment, as returned by :func:`_command_words`.

    Returns:
        The function name for ``name() {``, ``name () {`` and ``function name {`` forms.

    Examples:
        >>> _function_name(["_cq()", "{"])
        '_cq'
        >>> _function_name(["function", "cleanup", "{"])
        'cleanup'
        >>> _function_name(["echo", "hi"]) is None
        True
    """
    name = ""
    if words and words[0] == "function" and len(words) > 1:
        name = words[1].removesuffix("()")
    elif words and words[0].endswith("()"):
        name = words[0][:-2]
    elif len(words) > 1 and words[1] == "()":
        name = words[0]
    return name if _FUNCTION_NAME.fullmatch(name) else None


def _function_body(words: list[str]) -> list[str]:
    """Return the words after a function definition's name and opening brace.

    Examples:
        >>> _function_body(["f()", "{", "rm", "-rf", "x"])
        ['rm', '-rf', 'x']
        >>> _function_body(["function", "f", "{", "echo", "hi"])
        ['echo', 'hi']
    """
    start = 2 if words[0] == "function" or (len(words) > 1 and words[1] == "()") else 1
    body = words[start:]
    while body and body[0] in ("{", "("):
        body = body[1:]
    return body


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


def _case_line_arm(text: str) -> str | None:
    """Return the command text of a ``case`` arm written on the ``case … in`` line itself, or None.

    Examples:
        >>> _case_line_arm('case "$X" in a) mv x y')
        ' mv x y'
        >>> _case_line_arm('case "$X" in') is None
        True
        >>> _case_line_arm('case "$X" in a') is None
        True
        >>> _case_line_arm("echo a) b") is None
        True
    """
    if _command_words(text)[:1] != ["case"]:
        return None
    keyword = _CASE_IN.search(text)
    if keyword is None:
        return None
    after = text[keyword.end() :]
    end = _case_label_end(after)
    return after[end:] if end is not None else None


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
        if case_depth:
            label_end = _case_label_end(text)
            if label_end is not None:
                text = text[label_end:]
            elif _is_case_alternative(segments, index):
                continue
        words = _command_words(text)
        if words[:1] == ["case"]:
            case_depth += 1
            words = _case_arm_after_in(text)
        elif words[:1] == ["esac"]:
            case_depth = max(case_depth - 1, 0)
        offender = _words_unlisted(words, known)
        if offender is not None:
            return offender
    return None


def _heredoc_operators(line: str) -> list[tuple[str, bool, bool]]:
    """Return the heredocs a line opens, outside quotes, in order.

    Args:
        line: One physical line.

    Returns:
        ``(delimiter, quoted, strips_tabs)`` per ``<<WORD`` / ``<<-WORD`` / ``<<'WORD'``; a ``<<<`` here-string
        opens none.

    Examples:
        >>> _heredoc_operators("cat <<'EOF' > out")
        [('EOF', True, False)]
        >>> _heredoc_operators('echo "<<EOF"; grep x <<< "$v"')
        []
    """
    found: list[tuple[str, bool, bool]] = []
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote is not None:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif (match := _HEREDOC_OPERATOR.match(line, index)) is not None:
            found.append((match.group(3), bool(match.group(2)), bool(match.group(1))))
            index = match.end()
            continue
        index += 1
    return found


def _split_heredocs(command: str) -> tuple[str, list[str], bool]:
    """Separate heredoc bodies, which are data, from the commands around them.

    An unquoted delimiter leaves ``$(...)`` and backticks in the body live, so those substitutions are returned to be
    judged; a quoted delimiter makes the body literal.

    Args:
        command: A command or block.

    Returns:
        Tuple of (text without heredoc bodies and delimiter lines, live substitution bodies, True when a heredoc
        never reaches its delimiter).

    Examples:
        >>> _split_heredocs("cat <<EOF > f\\nhello $(date)\\nEOF\\necho done")
        ('cat <<EOF > f\\necho done', ['date'], False)
        >>> _split_heredocs("cat <<'EOF'\\n$(rm x)\\nEOF")
        ("cat <<'EOF'", [], False)
        >>> _split_heredocs("cat <<EOF\\nno end")[2]
        True
    """
    kept: list[str] = []
    live: list[str] = []
    pending: list[tuple[str, bool, bool]] = []
    for line in command.split("\n"):
        if pending:
            delimiter, quoted, strips_tabs = pending[0]
            if (line.lstrip("\t") if strips_tabs else line) == delimiter:
                pending.pop(0)
            elif not quoted:
                live.extend(_heredoc_substitutions(line))
            continue
        kept.append(line)
        pending.extend(_heredoc_operators(line))
    return "\n".join(kept), live, bool(pending)


def _heredoc_substitutions(line: str) -> list[str]:
    """Return the bodies of ``$(...)`` and backtick substitutions in a heredoc line, where quotes are literal.

    Examples:
        >>> _heredoc_substitutions("it's $(date) and `whoami`")
        ['date', 'whoami']
    """
    bodies: list[str] = []
    index = 0
    while index < len(line):
        read = _read_substitution(line, index)
        if read is None:
            index += 1
            continue
        bodies.append(read[0])
        index = read[1]
    return bodies


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
