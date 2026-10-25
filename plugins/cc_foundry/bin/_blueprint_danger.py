"""Classify shell blueprint text without executing commands.

Purpose: Keep shell lexing and danger classification together behind the manifest builder's public API. Scope: Read
shell words, compound commands, substitutions, lookup writes, name operands and text-only formats. Usage: Imported by
build_blueprint_manifest.py; the JavaScript allow hooks mirror the same static contracts. Outputs: Boolean danger
verdicts and parsed shell text for the manifest builder's completeness and allowlist gates. Failure: Unmodeled syntax
and unsafe writes are refused; this module never executes the command text it examines. Used by:
build_blueprint_manifest.py and its existing audit and hook parity tests through the unchanged public API.

This private sibling is shipped only with the foundry manifest builder. It has no CLI, filesystem writes or external
dependencies, and needs no propagation into plugins that consume generated manifests through their JavaScript hooks.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

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

#: Operators the completeness check reads at top level, longest first so ``;;&`` wins over ``;;`` and ``;``.
_LEX_OPERATORS = (
    *("&>>", ";;&", "<<<", "<<-", ">>", "<<", "<&", ">&", "<>", ">|", "&>", "&&", "||", ";;", ";&", "|&"),
    *("|", "&", ";", "(", ")", "<", ">"),
)
#: A redirection operator, with an optional descriptor number written against it (``2>``); it needs a target word.
#: Only a digit descriptor is read as one: a ``{name}>`` prefix assigns a variable and is refused by the grammar scan
#: (:class:`_LiveScan`), so here it stays a word of its own.
_LEX_REDIRECTION = re.compile(r"(?:[0-9]+)?(?:&>>|<<<|<<-|>>|<<|<&|>&|<>|>\||&>|<|>)")
#: A descriptor number written against a redirection operator, kept in one token with it.
_LEX_DESCRIPTOR = re.compile(r"[0-9]+")
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
    """Read a ``<(...)``, ``>(...)`` or zsh ``=(...)`` process substitution opening at ``index``.

    Only the two-character opener triggers this; a bare ``<`` or ``>`` redirection is
    left alone and is not a segment separator. zsh runs the body of ``=(...)`` to fill a
    temporary file only when the opener starts a word, so ``x=(a b)`` stays an array.

    Args:
        text: Full text.
        index: Index of the candidate ``<``, ``>`` or ``=``.

    Returns:
        Tuple of (inner text, index just past the closing parenthesis), or None when no
        process substitution opens at ``index``.

    Examples:
        >>> _read_process_substitution("cat <(rm -rf x)", 4)
        ('rm -rf x', 15)
        >>> _read_process_substitution("cat < file", 4) is None
        True
        >>> _read_process_substitution("cat =(rm -rf x)", 4)
        ('rm -rf x', 15)
        >>> _read_process_substitution("x=(a b)", 1) is None
        True
    """
    if text[index] in "<>" and text.startswith("(", index + 1):
        return _read_delimited(text, index + 2, "(", ")")
    word_start = index == 0 or text[index - 1].isspace() or text[index - 1] in ";&|("
    if word_start and text.startswith("=(", index):
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
    text: str,
    read_substitution: Callable[[str, int], tuple[str, int] | None] | None = None,
    substitution_spans: list[tuple[str, int]] | None = None,
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
        substitution_spans: Optional destination for bodies and their exact starting offsets.

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
                if substitution_spans is not None:
                    substitution_spans.append((read[0], index + (1 if char == "`" else 2)))
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
            if substitution_spans is not None:
                substitution_spans.append((read[0], index + (1 if char == "`" else 2)))
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


#: A redirection operator with its optional file-descriptor number: ``2>``, ``>>``, ``<<<``, ``&>``, ``>&``, ``<>``.
#: The descriptor-allocating ``{name}>`` form is no prefix here: it assigns ``name`` a number, so the grammar scan
#: refuses it (:data:`_FD_VARIABLE_PREFIX`) and this reader keeps ``{name}`` as a word.
_REDIRECTION_OPERATOR = re.compile(r"(?:[0-9]+|&)?(?:<<<|<<-?|<>|<&|>&|>>|>\||<|>)")
#: A word written right against a redirection operator as a brace group (``{name}>``, ``{IFS}<``): bash and zsh
#: allocate a descriptor and assign its number to the name, a variable write no ``=`` shows. Only a digit
#: descriptor (``2>``) is admitted.
_FD_VARIABLE_PREFIX = re.compile(r"\{[^}]*\}")
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

    The word ends at an unquoted space, tab or newline, at a redirection operator, or at an unquoted ``(``/``)``,
    which is a word of its own. Other controls and Unicode separators belong to the word. ``NAME=(…)`` array
    assignments and expansions are read whole.

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
        if char in " \t\n" or (char in "<>" and text[index + 1 : index + 2] != "("):
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
    return [word for word, _ in _shell_word_spans(segment)]


def _shell_word_spans(segment: str) -> list[tuple[_ShellWord, int]]:
    """Return each word :func:`shell_words` reads, with the index just past it in ``segment``.

    Examples:
        >>> [(word.raw, end) for word, end in _shell_word_spans("case 'a in b' in x) y")]
        [('case', 4), ("'a in b'", 13), ('in', 16), ('x', 18), (')', 19), ('y', 21)]
    """
    spans: list[tuple[_ShellWord, int]] = []
    index = 0
    while index < len(segment):
        if segment[index] in " \t\n" or segment.startswith("\\\n", index):
            index += 2 if segment[index] == "\\" else 1
            continue
        operator = _REDIRECTION_OPERATOR.match(segment, index)
        if operator and segment[operator.end() : operator.end() + 1] != "(":
            index = operator.end()
            while index < len(segment) and segment[index] in " \t\n":
                index += 1
            if index < len(segment):
                index = _read_word(segment, index)[1]
            continue
        word, end = _read_word(segment, index)
        if end == index:
            word, end = _ShellWord(segment[index], segment[index], False, False), index + 1
        spans.append((word, end))
        index = end
    return spans


def _command_start_index(words: list[_ShellWord]) -> int:
    """Return the index of the first word past reserved words, grouping and leading assignments, as written.

    Examples:
        >>> _command_start_index(shell_words('then FOO="a b" ( rm x'))
        3
    """
    index = 0
    while index < len(words):
        raw = words[index].raw
        if raw in SHELL_RESERVED_WORDS or not raw.strip("(){}") or _LEADING_ASSIGNMENT.match(raw):
            index += 1
        else:
            break
    return index


def _command_start_words(words: list[_ShellWord]) -> list[_ShellWord]:
    """Return the words from the command word on, past reserved words, grouping and leading assignments.

    Examples:
        >>> [word.value for word in _command_start_words(shell_words('then FOO="a b" ( rm x'))]
        ['rm', 'x']
    """
    return words[_command_start_index(words) :]


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
    """Report whether a ``printf``/``print`` writes a variable through a leading ``-v`` option.

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
    # This callback also judges commands carried by wrappers and compound syntax, using the existing resolver.
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
#: ``zparseopts``, ``getln``), or run text built at run time or read from a file (``eval``, ``source``; ``.`` is
#: judged in command position only, see :data:`_BUILTIN_RUNNERS`). Any of them can point a command name at another
#: program, and no shipped block runs one, so each is denied by name rather than judged by its arguments.
REBIND_COMMANDS: frozenset[str] = frozenset(
    {
        *("hash", "builtin", "enable", "disable", "alias", "unalias", "functions", "autoload", "zmodload"),
        *("emulate", "mapfile", "readarray", "vared", "zparseopts", "getln", "eval", "source"),
    }
)
#: Deferring commands that can run a shell builtin, so ``command . file`` sources a file like ``. file`` does. ``.``
#: is checked only as the first operand of one of these: elsewhere it is a directory (``find . -name x``).
_BUILTIN_RUNNERS = frozenset({"command", "noglob", "nocorrect", "-", "time"})
#: Any mention of a table that maps a name to what it runs: zsh ``commands``, ``functions``, ``aliases`` and their
#: ``galiases``/``saliases``/``dis_*`` siblings, ``fpath``/``FPATH``, and bash ``BASH_CMDS``/``BASH_ALIASES``. An
#: option prefix attached to the name (``set -Acommands``, ``printf -vBASH_CMDS[x]``) counts too. No shipped block
#: reads one either, so every mention is treated as a rebind instead of telling reads from writes.
_COMMAND_TABLE_USE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:[-+][A-Za-z]*)?"
    r"(?:commands|functions|aliases|galiases|saliases|dis_[a-z_]+|fpath|FPATH|BASH_CMDS|BASH_ALIASES)"
    r"(?![A-Za-z0-9_./-])"
)
#: An expansion that treats a variable's value as code or as a name: zsh ``${(P)n::=…}`` and bash ``${!n:=…}``
#: assign through a name held in another variable; zsh ``${(e)x}``, ``${(%)x}`` and ``${(#)x}`` and bash
#: ``${x@P}`` evaluate the value (command substitution, prompt or arithmetic expansion); zsh ``${~x}``/``$~x``
#: globs it, so a ``(e:…:)`` qualifier in the value runs. Stored text is inert to the lexer, so each form is refused.
_INDIRECT_EXPANSION = re.compile(r"\$\{(?:\([^)]*[Pe%#]|!)|\$\{[^}]*@P|\$\{?~")
#: Variables that decide which program a command name runs or which library it loads (``PATH``, zsh ``path``,
#: ``LD_*``, ``DYLD_*``), which file a shell runs at start-up (``BASH_ENV``, ``ENV``, ``ZDOTDIR``), text a shell
#: expands as code later (prompt strings, ``PROMPT_COMMAND``, zsh hook arrays), how the shell splits words
#: (``IFS``), or which options are on (zsh ``options``). ``hooks/sentinel-read-allow.js``'s ``SENSITIVE_VARS`` holds
#: every other name here (``IFS`` has its own check there), so the shape lane refuses the same ``read`` targets.
_LOOKUP_VARIABLES = (
    r"PATH|path|CDPATH|cdpath|BASH_ENV|ENV|ZDOTDIR|SHELLOPTS|BASHOPTS|GLOBIGNORE|PROMPT_COMMAND"
    r"|LD_PRELOAD|LD_LIBRARY_PATH|LD_AUDIT|DYLD_[A-Z_]+|PS[0-4]|PROMPT[0-4]?|RPROMPT2?|RPS[12]|SPROMPT"
    r"|(?:precmd|preexec|chpwd|periodic|zshexit|zshaddhistory)_functions|IFS|options"
)
#: A word assigning a lookup variable (``PATH=…``, ``path+=(…)``, ``path[1]=…``), wherever it stands in a segment.
_LOOKUP_VARIABLE_ASSIGNMENT = re.compile(rf"(?:{_LOOKUP_VARIABLES})(?:\[[^\]]*\])?\+?=.*", re.DOTALL)
#: Refuse writes regardless of which operand a shell evaluates as arithmetic; comparisons are reads.
_LOOKUP_WRITE_TOKEN = re.compile(
    rf"(?<![A-Za-z0-9_])(?:(?:\+\+|--)(?:{_LOOKUP_VARIABLES})(?![A-Za-z0-9_])"
    rf"|(?:{_LOOKUP_VARIABLES})(?:\[[^\]]*\])?(?:\+\+|--|[ \t\n\r\v\f]*(?:"
    r"\*\*=|<<=|>>=|&&=|\|\|=|\^\^=|[+*/%&|^\-]=|=(?!=))))"
)
#: These complete flag prefixes cannot be arithmetic lvalues; their values are still scanned for other writes.
_LOOKUP_WRITE_FLAG = re.compile(rf"(?:\$?'|\")?--?(?:{_LOOKUP_VARIABLES})=(?!=)")
#: Closed format language: string widths, literal percent signs and three text escapes only.
_PRINTF_TEXT_FORMAT = re.compile(r"(?:[^%\\]|%%|%(?:-?[0-9]+)?s|\\[nt\\])*")

#: Only literal commands in this closed list receive inert-argument exemptions; mirror in both JS lanes.
_LOOKUP_TEXT_PROGRAMS = frozenset(
    {
        "python",
        "python3",
        "node",
        "git",
        "gh",
        "grep",
        "rg",
        "sed",
        "awk",
        "jq",
        "cat",
        "ls",
        "find",
        "mkdir",
        "cp",
        "mv",
        "date",
        "wc",
        "sort",
        "head",
        "tail",
        "tr",
        "cut",
        "diff",
        "uv",
        "pytest",
        "pre-commit",
    }
)

#: A word naming a lookup variable on its own, which assigns it only as an operand of a name-assigning command.
_LOOKUP_VARIABLE_NAME = re.compile(rf"(?:-[A-Za-z]*)?(?:{_LOOKUP_VARIABLES})(?:\[[^\]]*\])?")
#: A lookup variable named inside arithmetic, read (``$PATH``) or assigned (``PATH``).
_LOOKUP_ARITHMETIC_NAME = re.compile(rf"\$?(?:{_LOOKUP_VARIABLES})")
#: Commands that assign or unset a variable named by a bare operand (``read PATH``, ``export PATH``,
#: ``getopts o PATH``); elsewhere a bare ``path`` is an ordinary argument (``--match path``, ``echo read path``).
#: ``print``/``printf`` assign only through ``-v`` and ``set`` only through ``-A``, which their own checks refuse; a
#: loop keyword assigns only its loop variable (:data:`_LOOP_KEYWORDS`).
_NAME_ASSIGNERS = frozenset(
    {
        *("read", "getopts", "unset", "vared", "zparseopts", "zstyle", "zformat"),
        *("typeset", "declare", "local", "export", "readonly", "integer", "float", "let"),
    }
)
#: Loop keywords whose next word is the variable each pass assigns.
_LOOP_KEYWORDS = frozenset({"for", "foreach", "select"})
#: Commands that run the command word after them, past their own options: the name-assigner check looks through
#: them (``command read PATH``, ``noglob read PATH``).
_COMMAND_RUNNERS = frozenset({"command", "builtin", "exec", "noglob", "nocorrect", "-", "time"})
#: The option letters a runner may carry, none of which takes an operand: ``command -p``/``-v``/``-V``,
#: ``exec -c``/``-l``, ``time -p``. Any other (``exec -a NAME`` takes the next word as the name to run under)
#: leaves the command word unknown, so the segment is refused.
_RUNNER_OPTION_LETTERS = {"command": frozenset("pvV"), "exec": frozenset("cl"), "time": frozenset("p")}
#: Commands whose operands are only text to print or match: an operand spelled like an assignment
#: (``echo 'PATH=/tmp'``) assigns nothing there. Any other command may read ``NAME=value`` operands as assignments
#: (``env``, ``export``, ``make``), so its operands are judged like leading assignments.
_TEXT_COMMANDS = frozenset({"echo", "printf", "print", "grep", "egrep", "fgrep", "rg"})
#: Commands that make every later assignment to a name arithmetic: zsh ``integer``/``float`` and ``let``.
_ARITHMETIC_COMMANDS = frozenset({"integer", "float", "let"})
#: Commands that give a name the integer attribute through an ``-i`` option, after which assigning it is arithmetic.
_ATTRIBUTE_DECLARERS = frozenset({"typeset", "declare", "local", "export", "readonly"})
#: A word opening an array assignment (``NAME=(``, ``NAME+=(``) as written.
_ARRAY_ASSIGNMENT_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=\(")
#: A subscripted name (``a[…]``) and the subscripts that hold no arithmetic able to assign or run anything.
_SUBSCRIPT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\[([^\]]*)\]")
_PLAIN_SUBSCRIPT = re.compile(r"[0-9]+|[@*]|\$?[A-Za-z_][A-Za-z0-9_]*")
#: Shell options a block may switch, by exact unquoted name; any other (``GLOB_SUBST`` in any spelling, which globs
#: every plain ``$x``) is refused rather than matched by spelling.
_SAFE_SHELL_OPTIONS = frozenset({"pipefail", "errexit", "nounset", "nomatch"})
#: Commands whose operands are option names.
_OPTION_SETTERS = frozenset({"setopt", "unsetopt", "shopt"})
#: A ``set`` short-option cluster limited to exit-on-error, unset-variable, trace, allexport and ``-o``.
_SET_FLAG_CLUSTER = re.compile(r"[-+][euxao]+")
#: A shell's short-option cluster that takes an option name as its next word (``-o``, ``-xo``, bash ``-O``).
_SHELL_OPTION_CLUSTER = re.compile(r"[-+][A-Za-z]*[oO][A-Za-z]*")
#: Long options a shell interpreter may carry; zsh reads ``--globsubst`` and the like as option names.
_SHELL_LONG_OPTIONS = frozenset({"--", "--norc", "--noprofile", "--login"})
#: The first character of a word's value (:attr:`_ShellWord.value`: quotes and escapes resolved, expansions, tilde,
#: globs and braces kept as written) that ends a shell's options: a literal letter, digit, ``_``, ``.`` or ``/``.
#: Every word the shell builds from it starts with that same character, so none can be a flag. Any other lead
#: (``-``, ``$``, a backtick, ``~``, ``=``, ``(``, ``<``, brace or glob) may yield a word starting ``-`` or ``+``
#: (``'-o'``, ``"$F"``, ``~`` with ``HOME=-o``, a file named ``-o``).
_OPTION_OPERAND_LEAD = re.compile(r"[A-Za-z0-9_./]")
#: An opener of command substitution or bash ``$[…]`` arithmetic, whose text runs when an expansion evaluates it.
_CODE_OPENER = re.compile(r"\$[(\[]|`")
#: A heredoc operator in the only forms the reader models: ``<<`` or ``<<-``, optional blanks, then an identifier
#: delimiter, bare or wholly in one pair of quotes, ending the word. ``<< \EOF``, ``<<$'EOF'``, ``<<E"OF"`` and
#: ``<<EOF.`` are other delimiters to the shell and are refused.
_HEREDOC_OPENING = re.compile(
    r"<<(-?)[ \t]*(?:([A-Za-z_][A-Za-z0-9_]*)|'([A-Za-z_][A-Za-z0-9_]*)'|\"([A-Za-z_][A-Za-z0-9_]*)\")"
    r"(?=[ \t;&|<>)\n]|$)"
)
#: The parameter a ``${…}`` expansion reads: a name, a positional or special parameter, with an optional ``[@]``,
#: ``[*]`` or numeric subscript.
_PARAMETER_NAME = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[?#@*$-])(?:\[(?:@|\*|[0-9]+)\])?")
#: A length expansion: ``#`` before any admitted parameter (``${#NAME}``, ``${#1}``, ``${#?}``, ``${#a[0]}``).
_PARAMETER_LENGTH = re.compile(rf"#(?:{_PARAMETER_NAME.pattern})")
#: Operators that read a parameter without assigning it: default, alternative, prefix or suffix removal, replacement.
_PARAMETER_OPERATOR = re.compile(r":-|:\+|-|\+|##|#|%%|%|//|/")
#: A substring offset and length in digits; any other offset is arithmetic.
_PARAMETER_OFFSET = re.compile(r":[0-9]+(?::[0-9]+)?")
#: Arithmetic text that assigns, shifts or indexes (``=``, ``+=``, ``++``, ``--``, ``<<``, ``,``, ``[``) or spans
#: lines, none of which a shipped block uses.
_ARITHMETIC_REFUSED = re.compile(r"\+\+|--|<<|>>|(?<![=!<>])=(?!=)|[\n,\[]")
#: An arithmetic operand: a number, a name, ``$NAME``, or a numeric special parameter (``$?`` ``$#`` ``$$`` ``$!``
#: ``$-``), each of which stands for a value a shipped block already reads (``exit $?``).
_ARITHMETIC_OPERAND = re.compile(r"[0-9]+|\$?[A-Za-z_][A-Za-z0-9_]*|\$[?#$!*@-]")
_ARITHMETIC_BINARY = re.compile(r"&&|\|\||<=|>=|==|!=|[-+*/%<>?:]")
#: Commands whose count operand zsh evaluates as an arithmetic expression (bash reads it as a plain number, but the
#: Bash tool's shell is zsh on macOS): ``shift``/``break``/``continue``/``return``/``exit`` (zshbuiltins, each "an
#: arithmetic expression n") and ``repeat`` (zshmisc, "treated as an arithmetic expression"). An assignment spelled
#: into the count (``exit x,PATH=1``) would write a name.
_ARITHMETIC_COUNT_COMMANDS = frozenset({"shift", "break", "continue", "return", "exit", "repeat"})
#: The comparison operators of ``[[ … ]]`` whose two operands undergo arithmetic evaluation ("as if enclosed in
#: ``$((…))``": zshmisc Conditional Expressions, bash CONDITIONAL EXPRESSIONS ``arg1 OP arg2``). ``test``/``[`` take
#: integers only (zshbuiltins: "arithmetic operators expect integer arguments rather than arithmetic expressions"),
#: so they are not here.
_CONDITIONAL_ARITHMETIC_OPS = frozenset({"-eq", "-ne", "-lt", "-le", "-gt", "-ge"})
#: Special parameters zsh gives the integer attribute by default (``typeset +i``: zshparam names each as "Integer"
#: or a count), so a plain ``NAME=value`` assignment evaluates value as arithmetic (zshparam "the value is subject to
#: arithmetic evaluation"). A run-time count written into one (``SECONDS=1,PATH=1``) would write a name. bash
#: integer-attributes only ``EUID``/``UID``/``PPID`` by default, so this is the zsh superset.
_INTEGER_PARAMETERS = (
    "ARGC|COLUMNS|EGID|ERRNO|EUID|FUNCNEST|GID|HISTCMD|HISTSIZE|KEYTIMEOUT|LINENO|LINES|LISTMAX|MAILCHECK"
    "|OPTIND|PPID|RANDOM|SAVEHIST|SECONDS|SHLVL|TRY_BLOCK_ERROR|TRY_BLOCK_INTERRUPT|TTYIDLE|UID|ZLE_RPROMPT_INDENT|ZSH_SUBSHELL"
)
#: A ``NAME=`` or ``NAME+=`` assignment to an integer-typed special parameter.
_INTEGER_ASSIGNMENT = re.compile(rf"(?:{_INTEGER_PARAMETERS})\+?=")
#: An array-literal element key (``a=([key]=value)``), evaluated in arithmetic context (zshparam ``set -A`` third
#: form, bash ``name[subscript]=value``).
_ARRAY_ELEMENT_KEY = re.compile(r"\[([^\]]*)\]\+?=")
#: A zsh ``$name[…]`` subscript, live inside double quotes too. Its text is arithmetic, admitted only when made of
#: names, numbers, ``@``/``*``, operators that read (``+ - * / % ^ ! < > ? : ,``), ``.`` and the ``[`` of a
#: bracket class (shipped ``"$PLUGIN[^/]*"``, ``"$PR_NUM[[:space:]]"``); no ``=``, quote, expansion, parenthesis
#: or blank. Increments and a lookup variable (:data:`_LOOKUP_ARITHMETIC_NAME`) are refused within it.
_ZSH_SUBSCRIPTED = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\[([^\]]*)")
#: What may follow a ``$`` in live text besides the ``${``/``$(``/backtick openers read on their own: a name or digit,
#: a special parameter (``$?`` ``$#`` ``$@`` ``$*`` ``$$`` ``$!`` ``$-``), or a character that opens no expansion in
#: bash or zsh, where the ``$`` is text. A quote also counts inside double quotes and heredoc bodies, where ``$"`` and
#: ``$'`` are text. Every other form — zsh ``$~x`` ``$=x`` ``$^x``, ``$[…]`` — is outside the grammar.
_DOLLAR_FOLLOW = re.compile(r"[A-Za-z0-9_?#@*$!-]|[ \t\n/.,:;|&)<>\\%}\]]|\Z")
_SUBSCRIPT_TEXT = re.compile(r"[A-Za-z0-9_@*^/!,.:?<>%+\[-]*")
_SUBSCRIPT_REFUSED = re.compile(rf"\+\+|--|(?<![A-Za-z0-9_])(?:{_LOOKUP_VARIABLES})(?![A-Za-z0-9_])")
#: A word that a following ``()`` makes a function definition.
_FUNCTION_WORD = re.compile(r"[A-Za-z0-9_.:-]+")
#: Reserved words after which a ``(`` still opens a subshell (``if (…)``, ``! (…)``, ``{ (…)``), as written.
_GROUP_OPENERS = frozenset({*SHELL_RESERVED_WORDS, "{"})


def _mask_single_quoted(text: str) -> str:
    """Hide ordinary single-quoted spans while retaining double-quoted and ANSI-C expansion spellings."""
    out = list(text)
    index, double = 0, False
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            double = not double
        if not double and text.startswith("$'", index):
            index = _read_ansi_c(text, index + 2)[1]
            continue
        if not double and text[index] == "'":
            end = text.find("'", index + 1)
            end = len(text) if end < 0 else end + 1
            out[index:end] = " " * (end - index)
            index = end
            continue
        index += 1
    return "".join(out)


def _writes_command_table(command: str) -> bool:
    """Detect command-table rebinding and indirect expansion.

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

    Check raw and dequoted names, excluding data heredocs but judging live substitutions.
    """
    text, live, unterminated = _split_heredocs(command)
    if unterminated:
        return True
    text = _mask_single_quoted(text)
    unquoted = text.replace("\\", "").replace("'", "").replace('"', "")
    return (
        any(_COMMAND_TABLE_USE.search(value) for value in (text, unquoted))
        or bool(_INDIRECT_EXPANSION.search(text))
        or any(_writes_command_table(body) for body in live)
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
    if head in _BUILTIN_RUNNERS and next((token for token in rest if not token.startswith("-")), "") == ".":
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
        >>> _segment_is_dangerous("case 'x in y' in *) eval x")
        True
        >>> _segment_is_dangerous("case x in x) if true { eval x }")
        True
        >>> _segment_is_dangerous("(shutdown)")
        True

    Brace syntax the reader does not follow (:func:`_command_reading`) is refused whatever it runs:

        >>> _segment_is_dangerous("case x { x) rm -rf /tmp/x "), _segment_is_dangerous(" } always { echo a")
        (True, True)
    """
    texts, unmodeled = _command_reading(segment)
    return unmodeled or any(_command_is_dangerous(text) for text in texts if not _is_case_syntax(text))


def _defined_function_names(segment: str) -> list[str] | None:
    """Return every name a segment defines as a function, or None when it defines none.

    Read from shell words, so a ``()`` inside quotes is not a definition. zsh defines one function per word before
    an unquoted ``()`` (``a b () {`` defines ``a`` and ``b``) and per word after ``function``, so all are returned.

    Examples:
        >>> _defined_function_names("_q() { x")
        ['_q']
        >>> _defined_function_names("ruff check () { x")
        ['ruff', 'check']
        >>> _defined_function_names("function a b { x")
        ['a', 'b']
        >>> _defined_function_names('printf "%s() but"') is None
        True
    """
    raws = [word.raw for word in _command_start_words(shell_words(segment))]
    if raws and raws[0] == "function":
        names: list[str] = []
        for raw in raws[1:]:
            if raw in ("(", "{"):
                break
            names.append(raw)
        return names
    for index in range(len(raws) - 1):
        if raws[index] == "(" and raws[index + 1] == ")":
            return raws[:index]
    return None


def _defines_shadowing_function(segment: str) -> bool:
    """Report whether a segment defines a function that may shadow a command.

    A function named like a command (``ruff () {``) changes what that name runs for the rest of the block, and the
    name-keyed argument checks then judge the wrong program. Helper names carry an underscore (``_q``,
    ``preflight_ok``); any other name, more than one name, or an anonymous function is refused.

    Examples:
        >>> _defines_shadowing_function("_q() { x")
        False
        >>> _defines_shadowing_function("ruff () { x")
        True
        >>> _defines_shadowing_function("echo hi")
        False
    """
    names = _defined_function_names(segment)
    if names is None:
        return False
    return len(names) != 1 or "_" not in names[0] or not _FUNCTION_NAME.fullmatch(names[0])


def _command_texts(segment: str) -> list[str]:
    """Return the segment and every command text it runs behind syntax that hides it from the command-word check.

    A ``case`` arm label (``b) rm x``), an arm written on the ``case … in`` line itself, a function definition
    (``f() { rm x``) and a zsh short or brace compound form (``if true { rm x }``) all put a non-command word first.
    Each hidden text is read the same way again, so an arm whose command is a compound, a function definition or
    another ``case`` is followed to its innermost command. This is the one reader of "what runs here" that every
    judge consumes (:func:`_judged_texts`): danger, rebinding, function shadowing and the allowlist gate.

    Args:
        segment: One unquoted segment.

    Returns:
        The segment first, then each hidden command text, each once.

    Examples:
        >>> _command_texts("b) rm -rf /")
        ['b) rm -rf /', ' rm -rf /']
        >>> _command_texts("case x in x) case y in y) rm -rf /")[1:]
        [' case y in y) rm -rf /', ' rm -rf /']
        >>> _command_texts("f() { rm -rf x")
        ['f() { rm -rf x', 'rm -rf x']
        >>> _command_texts("case x in x) if true { eval x }")[1:]
        [' if true { eval x }', 'eval x }', '']
        >>> _command_texts("echo hi")
        ['echo hi']
    """
    return _command_reading(segment)[0]


def _command_reading(segment: str) -> tuple[list[str], bool]:
    """Read a segment's command texts (:func:`_command_texts`) and whether it holds brace syntax the reader skips.

    A bare ``{`` or ``}`` word opens or closes a body only where the reader follows it to the command it runs:
    before the command word (a group at command position), in a compound text whose every brace is followed
    (:func:`_compound_bodies`), around a function body (:func:`_function_body`), or as the closing ``}`` run at the
    end of a text. Any other brace word (zsh ``case WORD {``, ``} always {``, a ``}`` with more words after it) is
    syntax the reader cannot place, so the segment is unmodeled whatever the brace hides. ``case`` syntax texts
    are judged by their arm, which the reader yields too.

    Returns:
        The command texts, the segment first, and True when any text holds an unfollowed brace word.

    Examples:
        >>> _command_reading("case x { x) rm -rf /tmp/x ")[1]
        True
        >>> _command_reading("{ echo a } always { rm -rf /tmp/x }")[1]
        True
        >>> _command_reading(" } always { rm -rf /tmp/x")[1]
        True
        >>> [_command_reading(text)[1] for text in ("{ echo a }", "if true { echo a } else { echo b }", "f() { x }")]
        [False, False, False]
    """
    texts: list[str] = []
    unmodeled = False
    # each text carries whether its braces are already followed: a compound body is a suffix of a text whose every
    # brace word starts another suffix
    queue: list[tuple[str, bool]] = [(segment, False)]
    while queue:
        text, followed = queue.pop(0)
        if text in texts:
            continue
        texts.append(text)
        words = _command_words(text)
        defines = _function_name(words) is not None
        if not (followed or defines or _is_case_syntax(text)) and _brace_unfollowed(text):
            unmodeled = True
        queue.extend((arm, False) for arm in _case_arm_chain(text)[1:])
        if defines:
            queue.append((" ".join(_function_body(words)), False))
        queue.extend((body, True) for body in _compound_bodies(text))
    return texts, unmodeled


def _brace_unfollowed(text: str) -> bool:
    """Report whether a command text holds a bare ``{`` or ``}`` word after its command word, other than a closing run.

    Words before the command word are grouping the reader passes over (:func:`_command_word_index`), and a ``}``
    run at the very end closes the bodies the text sits in. A text opening with a :data:`_COMPOUND_HEADS` word has
    every brace followed by :func:`_compound_bodies`. A runner option that hides the command word leaves any brace
    unplaced.

    Examples:
        >>> _brace_unfollowed("echo a } always { rm x }"), _brace_unfollowed("case x {")
        (True, True)
        >>> _brace_unfollowed("{ echo a }"), _brace_unfollowed(" } else { echo b")
        (False, False)
        >>> _brace_unfollowed("find . -exec x {} +")
        False
        >>> _brace_unfollowed("if true { a } else { b }")
        False
    """
    words = shell_words(text)
    raws = [word.raw for word in words]
    braces = [index for index, raw in enumerate(raws) if raw in ("{", "}")]
    if not braces or raws[0] in _COMPOUND_HEADS:
        return False
    start = _command_word_index(words)
    if start is None:
        return True
    closing = len(raws)
    while closing > 0 and raws[closing - 1] == "}":
        closing -= 1
    return any(start < index < closing for index in braces)


def _is_case_syntax(text: str) -> bool:
    """Report whether a text opens with ``case`` syntax rather than a command: an arm label or a ``case … in`` arm.

    A label opened by ``(`` is no exception only when the text is a subshell: ``(rm)`` is a command, so a text
    opening with ``(`` is judged as written too.

    Examples:
        >>> _is_case_syntax("b) rm x"), _is_case_syntax("case x in a) rm x"), _is_case_syntax("(rm)")
        (True, True, False)
        >>> _is_case_syntax("case x in")
        False
    """
    label = _case_label_end(text) is not None and not text.lstrip(" \t\n\r\f\v").startswith("(")
    return label or _case_line_arm(text) is not None


def _judged_texts(segment: str) -> list[str]:
    """Return the command texts of a segment (:func:`_command_texts`) that are commands rather than ``case`` syntax.

    A pattern is no word of the command it guards (``"uv run") R=(uv run)`` assigns before ``R``'s command), so a
    labelled text is judged by its arm, which the reader always yields too.

    Examples:
        >>> _judged_texts('case "$R" in "uv run") R=(uv run)')
        [' R=(uv run)']
        >>> _judged_texts("echo hi")
        ['echo hi']
    """
    return [text for text in _command_texts(segment) if not _is_case_syntax(text)]


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
        >>> _command_is_dangerous("9>/dev/null set -A n x")
        True
        >>> _command_is_dangerous("x=a; h${x:-/}sh -p /bin/echo ruff")
        True
        >>> _command_is_dangerous("unset -f ruff")
        True

    A decoded NUL is refused outright: bash ends a ``$'…'`` string there and zsh keeps it, so no single reading of
    the word is right for both shells. A pattern among a deferring command's words is refused like one in command
    position, since that word may be the command the wrapper runs. ``.`` sources a file like ``source``.

        >>> _command_is_dangerous(". ./state")
        True
        >>> _command_is_dangerous("command . ./state")
        True
        >>> _command_is_dangerous("find . -name x")
        False
    """
    words = _command_start_words(shell_words(segment))
    if not words:
        return False
    values = [word.value for word in words]
    if any("\x00" in value for value in values):
        return True
    head = _bare_word(values[0])
    if head in DANGER_COMMANDS or head in REBIND_COMMANDS or head == "." or _spelling_hides_name(words[0]):
        return True
    text_positions = _lookup_text_positions(words, _command_word_index(words), set())
    table_words = (
        word.value
        for index, word in enumerate(words)
        if not (index in text_positions and re.fullmatch(r"'[^']*'", word.raw, re.DOTALL))
    )
    if any(mark in _last_segment(values[0]) for mark in "$`") or any(_COMMAND_TABLE_USE.search(v) for v in table_words):
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
    # Preserve format quoting when a wrapper carries a formatter rather than running it directly.
    if any(
        _printf_writes_name(_bare_word(word.value), words, index)
        for index, word in enumerate(words[1:], 1)
        if _bare_word(word.value) in {"printf", "print"}
    ):
        return True
    return _deferred_is_dangerous(head, values[1:]) or any(is_dangerous(text) for text in _carried_commands(values))


def _mask_live_expansions(raw: str) -> str:
    """Return a word with each expansion the shell runs (``$(…)``, backticks, ``${…}``) replaced by ``_``.

    Single-quoted, ``$'…'`` and backslash-escaped text is kept as written, so it still decodes to what the word
    stores. Inside double quotes only the expansions are masked.

    Examples:
        >>> print(_mask_live_expansions("x=\\"$(grep -c '^```' f)\\"'$(id)'"))
        x="_"'$(id)'
    """
    out: list[str] = []
    index, double = 0, False
    while index < len(raw):
        end = _inert_span_end(raw, index, double)
        if end is not None:
            out.append(raw[index:end])
            index = end
            continue
        double = not double if raw[index] == '"' else double
        end = _read_expansion(raw, index)
        if end is not None and raw.startswith("${", index):
            # a `${x:-'…'}` operand is stored text too, so only the expansions nested in it are masked
            closed = raw[end - 1 : end] == "}"
            out.append(
                "${" + _mask_live_expansions(raw[index + 2 : end - 1 if closed else end]) + ("}" if closed else "")
            )
        else:
            out.append("_" if end is not None else raw[index])
        index = end if end is not None else index + 1
    return "".join(out)


def _inert_span_end(raw: str, index: int, double: bool) -> int | None:
    """Return the index past a backslash escape, ``'…'`` or ``$'…'`` opening at ``index``, or None.

    Inside double quotes only a backslash escape counts: there ``'`` is a plain character.

    Examples:
        >>> _inert_span_end("'a$(b)' c", 0, False)
        7
        >>> _inert_span_end("'a", 0, True) is None
        True
    """
    if raw[index] == "\\":
        return index + 2
    if double:
        return None
    body = _ansi_c_body(raw, index)
    if body is not None:
        return _read_ansi_c(raw, body)[1]
    if raw[index] == "'":
        close = raw.find("'", index + 1)
        return len(raw) if close < 0 else close + 1
    return None


def _stores_code(word: _ShellWord) -> bool:
    """Report whether an assignment word stores command-substitution text that its own expansion leaves inert.

    ``x='a[$(cmd)0]'`` assigns literal ``$(cmd)``; a later ``$((x))``, ``${(e)x}`` or prompt expansion runs it, and
    the lexer cannot see that far. The live expansions are masked first, so a code opener left in the decoded value
    can only come from quoted or escaped text.

    Examples:
        >>> _stores_code(shell_words("x='a[$(cmd)0]'")[0])
        True
        >>> _stores_code(shell_words('x="\\\\$(cmd)"')[0])
        True
        >>> _stores_code(shell_words("x=\\\\$\\\\(cmd)")[0])
        True
        >>> _stores_code(shell_words('x="$(git rev-parse HEAD)"')[0])
        False
        >>> _stores_code(shell_words("n=$(grep -c '^```' f)")[0])
        False
        >>> _stores_code(shell_words("echo '$(cmd)'")[1])
        False
    """
    if not _LEADING_ASSIGNMENT.match(word.value):
        return False
    return bool(_CODE_OPENER.search(_read_word(_mask_live_expansions(word.raw), 0)[0].value))


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


def _lookup_expression_spans(text: str) -> list[tuple[int, int]]:
    """Locate raw expression contexts, including those written inside quotes, that never receive text exemptions."""
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        opening = (
            "{"
            if text.startswith("${", index)
            else "("
            if text.startswith("((", index)
            else "["
            if text[index] == "["
            else ""
        )
        if not opening:
            index += 1
            continue
        begin = index
        stack = ["}"] if opening == "{" else [")", ")"] if opening == "(" else ["]"]
        index += 2 if opening != "[" else 1
        quote = ""
        while index < len(text) and stack:
            char = text[index]
            if char == "\\" and quote != "'":
                index += 2
                continue
            if quote:
                if char == quote:
                    quote = ""
            elif char in "'\"":
                quote = char
            elif char in "([{":
                stack.append({"(": ")", "[": "]", "{": "}"}[char])
            elif char == stack[-1]:
                stack.pop()
            index += 1
        spans.append((begin, index))
    return spans


def _lookup_regions(command: str) -> list[tuple[str, int]]:
    """Read command segments and nested substitutions with exact source offsets, avoiding textual lookalikes."""
    regions: list[tuple[str, int]] = []
    queue = [(command, 0)]
    while queue:
        text, offset = queue.pop()
        bodies: list[tuple[str, int]] = []
        separators, _, _ = _scan_top_level(text, substitution_spans=bodies)
        edges = [-1, *separators, len(text)]
        index = 0
        while index < len(edges) - 1:
            end = _conditional_end(text, edges, index)
            begin, finish = edges[index] + 1, edges[end]
            if text[begin:finish].strip():
                regions.append((text[begin:finish], offset + begin))
            index = end
        queue.extend((body, offset + begin) for body, begin in bodies)
    return regions


def _lookup_text_positions(words: list[_ShellWord], start: int | None, shadowed: set[str]) -> set[int]:
    """Identify inert operands of a literal unshadowed program, echo, or printf/print format."""
    if start is None or start >= len(words):
        return set()
    command = words[start]
    head = _bare_word(command.value)
    if (
        command.quoted
        or command.pattern
        or command.raw != command.value
        or any(c in command.raw for c in "$`~=\\")
        or head in shadowed
    ):
        return set()
    if head == "echo" or head in _LOOKUP_TEXT_PROGRAMS or re.fullmatch(r"[A-Za-z0-9_./+-]+\.(?:py|js)", command.raw):
        return set(range(start + 1, len(words)))
    operands = words[start + 1 :]
    if head == "read":
        positions = set()
        index = 0
        while index < len(operands):
            value = operands[index].raw
            if value == "--" or not value.startswith("-"):
                break
            if value == "-d" and index + 1 < len(operands):
                positions.add(start + 2 + index)
                index += 2
            elif value == "-t":
                index += 2
            else:
                index += 1
        return positions
    if head in ("printf", "print"):
        fmt = _printf_format_index(head, operands)
        if fmt is None or fmt < 0 or fmt >= len(operands):
            return set()
        if not _printf_writes_name(head, words, start):
            return set(range(start + 1 + fmt, len(words)))
        return {start + 1 + fmt}
    return set()


def _literal_lookup_data(word: _ShellWord, head: str, inert: bool) -> bool:
    """Keep literal format operands and delimiters out of expression scans."""
    return inert and (
        re.fullmatch(r"'[^']*'", word.raw, re.DOTALL) is not None
        or (_literal_word(word) and head in {"printf", "print", "read"})
    )


def _unusual_shell_character(text: str) -> bool:
    """Identify controls and non-ASCII Unicode spaces or format characters."""
    return any(
        (ord(char) < 32 and char not in "\t\n")
        or ord(char) == 127
        or (ord(char) > 127 and unicodedata.category(char) in {"Zs", "Zl", "Zp", "Cf"})
        for char in text
    )


def _unusual_lookup_write(value: str, head: str, inert: bool) -> bool:
    """Refuse unusual characters in expression spans and lookup-name writes, leaving assignment values as data."""
    if not _unusual_shell_character(value):
        return False
    if any(_unusual_shell_character(value[low:high]) for low, high in _lookup_expression_spans(value)):
        return True
    if inert:
        return False
    positions = [index for index, char in enumerate(value) if not _unusual_shell_character(char)]
    cleaned = "".join(value[index] for index in positions)
    if head in _NAME_ASSIGNERS | _LOOP_KEYWORDS and _LOOKUP_VARIABLE_NAME.fullmatch(cleaned):
        return True
    for match in _LOOKUP_WRITE_TOKEN.finditer(cleaned):
        # Include removed characters before the name, but never the assigned value after the operator.
        begin = positions[match.start() - 1] + 1 if match.start() else 0
        end = positions[match.end() - 1] + 1
        if _unusual_shell_character(value[begin:end]):
            return True
    return False


def _position_independent_write(command: str) -> bool:
    """Refuse raw and shell-decoded writes except inert operands, read prefixes, and assignment-flag prefixes.

    Single-quoted inert operands and quoted heredocs are data. Nested commands own operands; assignments stay live.
    """
    command, live, unterminated = _split_heredocs(command)
    if unterminated or any(_position_independent_write(body) for body in live):
        return True
    regions = _lookup_regions(command)
    shadowed: set[str] = set()
    for segment, _ in regions:
        for text in _judged_texts(segment):
            shadowed.update(_read_word(name, 0)[0].value for name in (_defined_function_names(text) or []))
            words = shell_words(text)
            start = _command_word_index(words)
            if start is not None and start < len(words) and _bare_word(words[start].value) == "alias":
                shadowed.update(word.value.partition("=")[0] for word in words[start + 1 :] if "=" in word.value)
    # Source-word keys are shared by compound suffixes; the innermost resolved command supplies their context.
    contexts: dict[tuple[int, int], tuple[bool, int, int, str]] = {}
    for segment, offset in regions:
        spans = _shell_word_spans(segment)
        raws = [word.raw for word, _ in spans]
        for text in _judged_texts(segment):
            words = shell_words(text)
            if not words or [word.raw for word in words] != raws[-len(words) :]:
                continue
            start = _command_word_index(words)
            head = _bare_word(words[start].value) if start is not None and start < len(words) else ""
            positions = _lookup_text_positions(words, start, shadowed)
            if head in ("printf", "print"):
                if _printf_writes_name(head, words, start):
                    return True
            for index, (word, end) in enumerate(spans[-len(words) :]):
                begin = offset + end - len(word.raw)
                flag = _LOOKUP_WRITE_FLAG.match(word.raw)
                prefix_end = begin + flag.end() if flag else begin
                if head == "read" and index < start and word.raw.startswith("IFS="):
                    prefix_end = begin + 4
                contexts[begin, offset + end] = (index in positions, prefix_end, len(segment), head)
    for (begin, end), (inert, prefix_end, _, head) in contexts.items():
        # Nested commands own their words; masking live expansions prevents a parent from judging their operands.
        raw = command[begin:end]
        value = _read_word(_mask_live_expansions(raw), 0)[0].value
        if _literal_lookup_data(_read_word(raw, 0)[0], head, inert):
            continue
        if _unusual_lookup_write(value, head, inert):
            return True
        flag = _LOOKUP_WRITE_FLAG.match(value)
        prefix = flag.end() if flag else 4 if raw.startswith("IFS=") and prefix_end == begin + 4 else 0
        spans = _lookup_expression_spans(value)
        for match in _LOOKUP_WRITE_TOKEN.finditer(value):
            if any(low < match.end() and match.start() < high for low, high in spans):
                return True
            if not inert and match.end() > prefix:
                return True
    protected = [
        (begin + low, begin + high)
        for (begin, end), (inert, _, _, head) in contexts.items()
        if not _literal_lookup_data(_read_word(command[begin:end], 0)[0], head, inert)
        for low, high in _lookup_expression_spans(command[begin:end])
    ]
    if any(_unusual_shell_character(command[begin:end]) for begin, end in protected):
        return True
    for match in _LOOKUP_WRITE_TOKEN.finditer(command):
        if any(begin < match.end() and match.start() < end for begin, end in protected):
            return True
        candidates = [
            (width, end - begin, inert, prefix_end)
            for (begin, end), (inert, prefix_end, width, _) in contexts.items()
            if begin <= match.start() < end
        ]
        if not candidates:
            return True
        _, _, inert, prefix_end = min(candidates)
        if not inert and match.end() > prefix_end:
            return True
    return False


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
        >>> is_dangerous("{fd}>/dev/null set -A n x"), is_dangerous("cat {IFS}</dev/null f")
        (True, True)

    Text that ends inside an unclosed quote, at top level or in a substitution body, is refused: the reader and the
    shell disagree about where its commands start, so no verdict on its segments can be trusted. So is a ``$'…'``
    string anywhere in it, assignment or redirection target included, holding an escape bash and zsh read differently
    (:func:`_read_ansi_c`), and any construct outside the grammar the reader models (:class:`_LiveScan`).

    Lookup-write tokens are refused before positional checks except proven inert operands and heredoc data. Live
    substitutions remain judged. A command that runs a body as shell code reads stdin and is refused by name.
    """
    if _position_independent_write(command):
        return True
    if _writes_command_table(command) or _quoting_unsettled(command) or _rebinds_through_words(command):
        return True
    text, live, _ = _split_heredocs(command)
    return any(_segment_is_dangerous(segment) for chunk in (text, *live) for segment in split_segments(chunk))


def _rebinds_through_words(command: str) -> bool:
    """Report whether ``command`` leaves the modeled grammar or any of its words rebinds a name or stores code.

    Heredoc bodies are data, so their words are left out: a quoted body is literal, and the only shells that would
    run one read stdin (``bash`` with no script, ``source``, ``eval``), which are refused by name. The expansions of
    an unquoted body are judged by the grammar scan, its substitutions also word by word.

    Examples:
        >>> _rebinds_through_words("PATH=/tmp")
        True
        >>> _rebinds_through_words("python - <<'PY'\\nx = f.strip()\\nPY")
        False
        >>> _rebinds_through_words("echo $(PATH=/tmp; ruff check)")
        True
    """
    if _unmodeled_construct(command):
        return True
    text, live, unterminated = _split_heredocs(command)
    # an unterminated body means the reader and the shell disagree about where commands resume
    if unterminated:
        return True
    chunks = [text, *live, *_scan_top_level(text)[1]]
    return any(_segment_rebinds(segment) for chunk in chunks for segment in split_segments(chunk))


def _segment_rebinds(segment: str) -> bool:
    """Report whether one segment rebinds a name through its words or stores code for a later expansion to run.

    Refused: a function that may shadow a command (:func:`_defines_shadowing_function`); a word assigning a lookup
    variable (:data:`_LOOKUP_VARIABLE_ASSIGNMENT`) wherever it stands, except an ``IFS=…`` prefix of a ``read``; an
    assignment storing code (:func:`_stores_code`) or assigning through an arithmetic subscript; and the name-writing
    commands judged by :func:`_command_writes_name`.

    Examples:
        >>> _segment_rebinds("read -r PATH</tmp/input")
        True
        >>> _segment_rebinds('echo "$PATH" "path not found"')
        False
        >>> _segment_rebinds("python x --match path")
        False
        >>> _segment_rebinds("ruff check () { x")
        True
        >>> _segment_rebinds("IFS='=' read -r k v")
        False
        >>> _segment_rebinds("IFS=, ls")
        True
        >>> _segment_rebinds("a[PATH=1]=0")
        True
        >>> _segment_rebinds("case x in x) function ruff { :")
        True
        >>> _segment_rebinds('case "$R" in "uv run") R=(uv run)')
        False
    """
    return any(_text_rebinds(text) for text in _judged_texts(segment))


def _text_rebinds(text: str) -> bool:
    """Report whether one command text, ``case`` syntax already read off (:func:`_judged_texts`), rebinds a name.

    Examples:
        >>> _text_rebinds("read -r PATH")
        True
        >>> _text_rebinds("echo read path")
        False
    """
    if _defines_shadowing_function(text):
        return True
    words = shell_words(text)
    values = [word.value for word in words]
    start = _command_word_index(words)
    if start is None:
        return True
    head = _bare_word(values[start]) if start < len(values) else ""
    text_positions = _lookup_text_positions(words, start, set())
    for index, word in enumerate(words):
        prefix_ifs = word.value.startswith("IFS=") and index < start and head == "read"
        assigning = index < start or head not in _TEXT_COMMANDS
        if (
            assigning
            and _LOOKUP_VARIABLE_ASSIGNMENT.fullmatch(word.value)
            and not prefix_ifs
            and index not in text_positions
        ):
            return True
        if _assignment_arithmetic_unmodeled(word, assigning):
            return True
        # `NAME=(…)` is an array only before the command word or after a declaration builtin; anywhere else zsh
        # reads its `(…)` as glob qualifiers, which can run code
        if index >= start and head not in _ATTRIBUTE_DECLARERS and _ARRAY_ASSIGNMENT_WORD.match(word.raw):
            return True
        if index not in text_positions and (_stores_code(word) or _assigns_through_subscript(word)):
            return True
    return _command_writes_name(words, start)


def _case_arm_chain(segment: str) -> list[str]:
    """Return a segment and each text left as its ``case`` arm patterns come off one by one; the last is its command.

    A pattern is no word of the command it guards (``"uv run") R=(uv run)`` assigns before ``R``'s command), and an
    arm may open another ``case`` (``case x in x) case y in y) cmd``), so labels come off until none is left. Every
    reader that skips a label uses this chain, so a nested arm's command is never left unjudged.

    Examples:
        >>> _case_arm_chain('case "$R" in "uv run") R=(uv run)')
        ['case "$R" in "uv run") R=(uv run)', ' R=(uv run)']
        >>> _case_arm_chain("case x in x) case y in y) read PATH")[1:]
        [' case y in y) read PATH', ' read PATH']
        >>> _case_arm_chain("b) c) rm x")[-1]
        ' rm x'
        >>> _case_arm_chain("echo hi")
        ['echo hi']
    """
    chain = [segment]
    while True:
        text = chain[-1]
        arm = _case_line_arm(text)
        if arm is None:
            end = _case_label_end(text)
            arm = text[end:] if end else None
        if arm is None or arm == text:
            return chain
        chain.append(arm)


def _command_word_index(words: list[_ShellWord]) -> int | None:
    """Return the index of the command word, past reserved words, grouping, leading assignments and runners.

    Reserved words, grouping and assignments are recognized as written: a quoted ``'X=1'`` or ``'if'`` is the
    command word. A runner (``command``, ``builtin``, ``exec``, ``noglob``, ``time``, …) and its options are passed
    over, so the command it runs is the one judged. A runner option outside :data:`_RUNNER_OPTION_LETTERS` may take
    the next word as its operand, so the command word is unknown: None.

    Examples:
        >>> _command_word_index(shell_words("x=1 command -p read PATH"))
        3
        >>> _command_word_index(shell_words("echo read path"))
        0
        >>> _command_word_index(shell_words("'X=1' a=(x)"))
        0
        >>> _command_word_index(shell_words("exec -a name zsh")) is None
        True
    """
    values = [word.value for word in words]
    index = 0
    while index < len(values):
        value, raw = values[index], words[index].raw
        if raw in SHELL_RESERVED_WORDS or not raw.strip("(){}") or _LEADING_ASSIGNMENT.match(raw):
            index += 1
        elif value == "env" and raw == "env":
            index += 1
            while index < len(values) and values[index].startswith("-"):
                option = words[index]
                if option.raw not in {"-i", "--ignore-environment", "--"}:
                    return None
                index += 1
                if option.raw == "--":
                    break
            while index < len(words) and _ASSIGNMENT.match(words[index].raw):
                if not _literal_word(words[index]):
                    return None
                index += 1
        elif value in _COMMAND_RUNNERS:
            index += 1
            letters = _RUNNER_OPTION_LETTERS.get(value, frozenset())
            while index < len(values) and values[index].startswith("-") and values[index] != "-":
                option = values[index]
                index += 1
                if option == "--":
                    break
                if not set(option[1:]) <= letters:
                    return None
        else:
            return index
    return index


def _name_operand_unmodeled(word: _ShellWord, assignment: bool = False) -> bool:
    """Require a literal safe name, allowing declaration values."""
    raw = word.raw
    if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]:
        raw = raw[1:-1]
    name = raw.partition("=")[0].removesuffix("+") if assignment and "=" in raw else raw
    return (
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
        or _LOOKUP_VARIABLE_NAME.fullmatch(name) is not None
        or _COMMAND_TABLE_USE.search(name) is not None
    )


def _builtin_names_unmodeled(head: str, operands: list[_ShellWord]) -> bool:
    """Validate name operands even after option terminators."""
    if head == "shift":
        return len(operands) > 1 or any(
            not _literal_word(word) or re.fullmatch(r"[0-9]+", word.value) is None for word in operands
        )
    if head == "getopts":
        return (
            len(operands) < 2
            or not _literal_word(operands[0])
            or operands[0].value.startswith("-")
            or _name_operand_unmodeled(operands[1])
        )
    if head not in _NAME_ASSIGNERS:
        return False
    assignment = head in _ATTRIBUTE_DECLARERS or head in {"integer", "float"}
    options = True
    index = 0
    while index < len(operands):
        word = operands[index]
        value = word.value
        index += 1
        if options and value == "--" and word.raw == "--":
            options = False
            continue
        if options and value.startswith(("-", "+")):
            if word.raw != value or re.fullmatch(r"[-+][A-Za-z]+", value) is None:
                return True
            if head == "read":
                if value == "-t":
                    if index >= len(operands) or re.fullmatch(r"[0-9]+", operands[index].raw) is None:
                        return True
                    index += 1
                elif value == "-d":
                    if index >= len(operands) or not _literal_word(operands[index]):
                        return True
                    index += 1
                elif value not in {"-a", "-A"} and re.fullmatch(r"-[ers]+", value) is None:
                    return True
            continue
        if _name_operand_unmodeled(word, assignment):
            return True
    return False


def _command_writes_name(words: list[_ShellWord], start: int) -> bool:
    """Report whether a segment's command writes a lookup variable by name or switches a shell option.

    Require literal safe names for builtins and loops; retain arithmetic, integer-attribute and shell-option checks.

    Examples:
        >>> _command_writes_name(shell_words("command read PATH"), 1)
        True
        >>> _command_writes_name(shell_words("echo read path"), 0)
        False
        >>> _command_writes_name(shell_words("for path in a"), 0)
        True
        >>> _command_writes_name(shell_words("echo for path in a"), 0)
        False
        >>> _command_writes_name(shell_words("typeset -i x"), 0)
        True
    """
    values = [word.value for word in words]
    head = _bare_word(values[start]) if start < len(values) else ""
    operands = values[start + 1 :]
    if head in _LOOP_KEYWORDS:
        # zsh loops over several names (`for a b in …`) and takes a `(…)` word list (`for x (a b)`)
        for word in words[start + 1 :]:
            if word.raw in ("in", "do") or word.raw.startswith("("):
                break
            if _name_operand_unmodeled(word):
                return True
    if _builtin_names_unmodeled(head, words[start + 1 :]):
        return True
    if head in _NAME_ASSIGNERS and any(
        _LOOKUP_VARIABLE_NAME.fullmatch(value) or _unplain_subscript(value) for value in operands
    ):
        return True
    if head in _ARITHMETIC_COMMANDS or (
        head in _ATTRIBUTE_DECLARERS and any(re.fullmatch(r"[-+][A-Za-z]*i[A-Za-z]*", value) for value in operands)
    ):
        return True
    if _evaluates_arithmetic(head, words, start):
        return True
    raws = [word.raw for word in words]
    # a shell a wrapper runs (`env zsh -o …`, `sudo bash -O …`) takes its options from the words after it
    return _option_unmodeled(head, raws[start + 1 :]) or any(
        _bare_word(value) in SHELL_INTERPRETERS and _option_unmodeled(_bare_word(value), raws[index + 1 :])
        for index, value in enumerate(values)
    )


def _unplain_subscript(value: str) -> bool:
    """Report whether a word holds a subscript other than a number, ``@``/``*`` or one name: that one is arithmetic.

    Examples:
        >>> _unplain_subscript("a[PATH=1]")
        True
        >>> _unplain_subscript("a[i]")
        False
    """
    return any(not _PLAIN_SUBSCRIPT.fullmatch(match.group(1)) for match in _SUBSCRIPT.finditer(value))


def _assigns_through_subscript(word: _ShellWord) -> bool:
    """Report whether an unquoted assignment word assigns through an arithmetic subscript (``a[PATH=1]=0``).

    Examples:
        >>> _assigns_through_subscript(shell_words("a[PATH=1]=0")[0])
        True
        >>> _assigns_through_subscript(shell_words("a[1]=0")[0])
        False
    """
    target = re.match(r"[A-Za-z_][A-Za-z0-9_]*\[[^\]]*\]\+?=", word.raw)
    return target is not None and _unplain_subscript(target.group())


def _arithmetic_word_unmodeled(word: _ShellWord) -> bool:
    """Report whether a word used as an arithmetic operand assigns a name or runs code when it is evaluated.

    Arithmetic evaluation re-expands the operand's text (zshexpn: ``exp`` "is subjected to parameter expansion,
    command substitution and arithmetic expansion before it is evaluated"), so a ``$(…)``, backtick or ``$[…]``
    stored in it by quoting runs on evaluation; the live expansions are masked first, leaving a code opener only where
    quoting or an escape stored one. What remains must pass the one arithmetic admit set
    (:func:`_arithmetic_unmodeled`): no assignment, comma, increment, ternary or lookup name.

    Examples:
        >>> _arithmetic_word_unmodeled(shell_words("1")[0])
        False
        >>> _arithmetic_word_unmodeled(shell_words("x,PATH=1")[0])
        True
        >>> _arithmetic_word_unmodeled(shell_words("'1+$(rm -rf /)'")[0])
        True
        >>> _arithmetic_word_unmodeled(shell_words('"$n"')[0])
        False
        >>> _arithmetic_word_unmodeled(shell_words("$?")[0])
        False
    """
    stored = _decode_operand(_mask_live_expansions(word.raw))
    return bool(_CODE_OPENER.search(stored)) or _arithmetic_unmodeled(word.value)


def _conditional_writes_name(words: list[_ShellWord], start: int) -> bool:
    """Report whether a ``[[ … ]]`` test evaluates an arithmetic operand that assigns a name.

    The two operands of a numeric comparison (:data:`_CONDITIONAL_ARITHMETIC_OPS`) undergo arithmetic evaluation, and
    the subscript of a ``-v name[idx]`` test is arithmetic too, so each is judged by the arithmetic admit set.

    Examples:
        >>> _conditional_writes_name(shell_words("[[ 1 -eq x,PATH=1 ]]"), 0)
        True
        >>> _conditional_writes_name(shell_words("[[ $n -gt 0 ]]"), 0)
        False
        >>> _conditional_writes_name(shell_words("[[ -v 'a[PATH=1]' ]]"), 0)
        True
        >>> _conditional_writes_name(shell_words("[[ -v arr[2] ]]"), 0)
        False
    """
    tokens = words[start:]
    for index, word in enumerate(tokens):
        if word.value in _CONDITIONAL_ARITHMETIC_OPS:
            if index > 0 and _arithmetic_word_unmodeled(tokens[index - 1]):
                return True
            if index + 1 < len(tokens) and _arithmetic_word_unmodeled(tokens[index + 1]):
                return True
        elif word.value == "-v" and index + 1 < len(tokens) and _unplain_subscript(tokens[index + 1].value):
            return True
    return False


def _count_writes_name(words: list[_ShellWord], start: int) -> bool:
    """Report whether a count command's operand (:data:`_ARITHMETIC_COUNT_COMMANDS`) assigns a name when evaluated.

    Only the first non-option operand is the count (``shift -p n``, ``repeat n body``); the rest are names or a loop
    body judged elsewhere.

    Examples:
        >>> _count_writes_name(shell_words("exit x,PATH=1"), 0)
        True
        >>> _count_writes_name(shell_words("exit $?"), 0)
        False
        >>> _count_writes_name(shell_words("shift"), 0)
        False
    """
    for word in words[start + 1 :]:
        if word.value.startswith("-") and len(word.value) > 1:
            continue
        return _arithmetic_word_unmodeled(word)
    return False


def _printf_format_index(head: str, operands: list[_ShellWord]) -> int | None:
    """Locate a format: None if absent, -1 for unsupported options.

    ``printf`` permits one bare ``--``; ``print`` permits ``-n``/``-r`` clusters and one ``-f`` until data or ``--``.

    Examples:
        >>> _printf_format_index("printf", shell_words("%d x"))
        0
        >>> _printf_format_index("printf", shell_words("-v n %d x"))
        -1
        >>> _printf_format_index("print", shell_words("-r x")) is None
        True
        >>> _printf_format_index("print", shell_words("-f %d x"))
        1
        >>> _printf_format_index("print", shell_words("-u 2 -f %d x"))
        -1
    """
    if head == "printf":
        index = 1 if operands and operands[0].raw == "--" else 0
        if index >= len(operands):
            return None
        value = operands[index].value
        return -1 if index == 0 and value.startswith("-") and not value.startswith("-%") else index
    fmt = None
    index = 0
    while index < len(operands):
        value = operands[index].value
        if value == "--":
            if operands[index].raw != "--" or (
                fmt is None and index + 1 < len(operands) and operands[index + 1].value.startswith("-")
            ):
                return -1
            return fmt
        if not value.startswith("-") or len(value) < 2:
            return fmt
        if re.fullmatch(r"-[nr]+", value) and _literal_word(operands[index]):
            index += 1
        elif re.match(r"^-[nr]*f", value) and fmt is None and _literal_word(operands[index]):
            option_end = value.index("f") + 1
            fmt = index if len(value) > option_end else index + 1
            index += 1 if len(value) > option_end else 2
        else:
            return -1
    return fmt


def _literal_word(word: _ShellWord) -> bool:
    """Report whether a word has a fixed value, with no live expansion or unquoted word generation."""
    if word.pattern or "\x00" in word.value:
        return False
    index, double = 0, False
    while index < len(word.raw):
        end = _inert_span_end(word.raw, index, double)
        if end is not None:
            index = end
            continue
        char = word.raw[index]
        if char in "$`" or (not double and char == "~"):
            return False
        if char == '"':
            double = not double
        index += 1
    return True


def _printf_writes_name(head: str, words: list[_ShellWord], start: int) -> bool:
    """Refuse formats outside the literal text-only language, including all numeric conversions.

    String formats, widths, %% and newline/tab/backslash escapes keep operands as data.

    Examples:
        >>> _printf_writes_name("printf", shell_words("printf %d x,PATH=1"), 0)
        True
        >>> _printf_writes_name("printf", shell_words("printf %n PATH"), 0)
        True
        >>> _printf_writes_name("printf", shell_words('printf "%d" "$n"'), 0)
        True
        >>> _printf_writes_name("printf", shell_words("printf '%s' 'PATH=x,y'"), 0)
        False
    """
    operands = words[start + 1 :]
    fmt_index = _printf_format_index(head, operands)
    if fmt_index is None:
        return head == "printf"
    if fmt_index < 0 or fmt_index >= len(operands):
        return True
    word = operands[fmt_index]
    fmt = word.value
    if head == "print" and re.match(r"^-[nr]*f", word.value):
        # Only an attached format loses its option prefix; a separate dash-leading format is checked whole.
        separate = fmt_index > 0 and re.fullmatch(r"-[nr]*f", operands[fmt_index - 1].value)
        if not separate:
            fmt = fmt.partition("f")[2]
    return not _literal_word(word) or _PRINTF_TEXT_FORMAT.fullmatch(fmt) is None


def _read_timeout_unmodeled(words: list[_ShellWord], start: int) -> bool:
    """Report whether a ``read -t`` timeout operand assigns a name when it is evaluated (zshbuiltins: "evaluated").

    The timeout follows a ``t`` option cluster, attached (``-t1``) or as the next word (``-t 1``).

    Examples:
        >>> _read_timeout_unmodeled(shell_words("read -t 1,PATH=1 x"), 0)
        True
        >>> _read_timeout_unmodeled(shell_words("read -r X"), 0)
        False
    """
    operands = words[start + 1 :]
    for index, word in enumerate(operands):
        value = word.value
        if value == "--" or not value.startswith("-"):
            return False
        match = re.fullmatch(r"-[A-Za-z]*t([A-Za-z0-9_,.$+*/%<>?:()!=-]*)", value)
        if match is None:
            continue
        if match.group(1):
            return _arithmetic_unmodeled(match.group(1))
        return index + 1 < len(operands) and _arithmetic_word_unmodeled(operands[index + 1])
    return False


def _evaluates_arithmetic(head: str, words: list[_ShellWord], start: int) -> bool:
    """Report whether a command evaluates one of its operands as arithmetic that assigns a name or runs code.

    Dispatches to the position's judge: ``[[ … ]]`` comparison operands, a count command's count, a ``printf`` or
    ``print -f`` numeric conversion or ``%n``, or a ``read -t`` timeout.

    Examples:
        >>> _evaluates_arithmetic("[[", shell_words("[[ 1 -eq x,PATH=1 ]]"), 0)
        True
        >>> _evaluates_arithmetic("exit", shell_words("exit 1"), 0)
        False
    """
    if head == "[[":
        return _conditional_writes_name(words, start)
    if head in _ARITHMETIC_COUNT_COMMANDS:
        return _count_writes_name(words, start)
    if head in ("printf", "print"):
        return _printf_writes_name(head, words, start)
    if head == "read":
        return _read_timeout_unmodeled(words, start)
    return False


def _assignment_arithmetic_unmodeled(word: _ShellWord, assigning: bool) -> bool:
    """Report whether an assignment word evaluates arithmetic that assigns a name: an integer parameter's value or an
    array-literal element key.

    The integer-parameter name is matched on ``word.value`` (quotes resolved), so ``export "SECONDS"=1,PATH=1``
    assigns it just as the shell does; the value after ``=`` is then judged as arithmetic (a quoted ``$(…)`` that
    re-expands is the run-time residual, as for any run-time value). The array-element key is read from ``word.raw``.

    Examples:
        >>> _assignment_arithmetic_unmodeled(shell_words("SHLVL=1,PATH=1")[0], True)
        True
        >>> _assignment_arithmetic_unmodeled(shell_words('"SECONDS"=1,PATH=1')[0], True)
        True
        >>> _assignment_arithmetic_unmodeled(shell_words("a=([PATH=1]=x)")[0], True)
        True
        >>> _assignment_arithmetic_unmodeled(shell_words("SECONDS=0")[0], True)
        False
        >>> _assignment_arithmetic_unmodeled(shell_words("a=([3]=x)")[0], True)
        False
    """
    if assigning:
        integer = _INTEGER_ASSIGNMENT.match(word.value)
        if integer is not None and _arithmetic_unmodeled(word.value[integer.end() :]):
            return True
    return _ARRAY_ASSIGNMENT_WORD.match(word.raw) is not None and any(
        _arithmetic_unmodeled(key.group(1)) for key in _ARRAY_ELEMENT_KEY.finditer(word.raw)
    )


def _option_unmodeled(head: str, operands: list[str]) -> bool:
    """Report whether a command switches a shell option outside :data:`_SAFE_SHELL_OPTIONS`.

    ``setopt``/``unsetopt``/``shopt`` take option names as operands; ``set`` and a shell interpreter take one after
    ``-o``/``+o`` (bash also ``-O``). Operands are the words as written and names compare exactly, so no spelling of
    ``GLOB_SUBST`` passes and a quoted name is refused. ``shopt`` flags are limited to ``-s``/``-u``/``-p``/``-q``/
    ``-o``, a ``set`` flag cluster to :data:`_SET_FLAG_CLUSTER`, and a shell's long option to
    :data:`_SHELL_LONG_OPTIONS`.

    Examples:
        >>> _option_unmodeled("unsetopt", ["nomatch"])
        False
        >>> _option_unmodeled("setopt", ['glob_s""ubst'])
        True
        >>> _option_unmodeled("setopt", ['"nomatch"'])
        True
        >>> _option_unmodeled("shopt", ["-sextglob"])
        True
        >>> _option_unmodeled("set", ["-euo", "pipefail"])
        False
        >>> _option_unmodeled("zsh", ["--globsubst", "-c", "x"])
        True

    A shell reads options up to its first operand, so a long option it may carry (``--norc``) and a cluster holding
    ``-c`` are passed over and the options after them judged too:

        >>> _option_unmodeled("zsh", ["--norc", "-o", "globsubst", "-c", "x"])
        True
        >>> _option_unmodeled("bash", ["-c", "-O", "extglob", "x"])
        True
        >>> _option_unmodeled("bash", ["-c", "x", "-O", "extglob"])
        False

    An option name attached to its flag is judged as that name, and the next word is no option name:

        >>> _option_unmodeled("zsh", ["-oglobsubst", "pipefail", "-c", "x"])
        True
        >>> _option_unmodeled("zsh", ["-onomatch", "-c", "x"])
        False

    Options are read as written, so a word that only becomes a flag once quotes, escapes, expansion or generation
    run (``'-o'``, ``\\-o``, ``"$F"``, ``{-o,x}``, ``~`` with ``HOME=-o``) cannot end the scan; it is refused. A word
    ends the scan only when its value, quotes removed, opens with a literal letter, digit, ``_``, ``.`` or ``/``:

        >>> _option_unmodeled("set", ["'-o'", "globsubst"])
        True
        >>> _option_unmodeled("bash", ['"$F"', "-c", "x"])
        True
        >>> _option_unmodeled("set", ["~", "globsubst"])
        True
        >>> _option_unmodeled("bash", ["build.sh", "-o", "x"])
        False
        >>> _option_unmodeled("bash", ["-c", "'echo x'"])
        False
    """
    if head in _OPTION_SETTERS:
        return any(
            value not in _SAFE_SHELL_OPTIONS and not (head == "shopt" and re.fullmatch(r"-[supqo]+", value))
            for value in operands
        )
    if head != "set" and head not in SHELL_INTERPRETERS:
        return False
    index = 0
    while index < len(operands):
        value = operands[index]
        if value in ("--", "-"):
            return False
        if not value.startswith(("-", "+")):
            # the first operand ends the options only when the lead of its value is literal text that keeps every
            # word built from it from starting `-` or `+`; an expansion, tilde, glob or brace lead stays as written
            return not _OPTION_OPERAND_LEAD.match(_read_word(value, 0)[0].value)
        if value.startswith("--") or not re.fullmatch(r"[-+][A-Za-z]+", value):
            if head == "set" or value not in _SHELL_LONG_OPTIONS:
                return True
            index += 1
        elif head == "set" and not _SET_FLAG_CLUSTER.fullmatch(value):
            # `set -A`/`+A` assigns an array, judged by its own check; any other flag is refused
            return value[1] != "A"
        elif _SHELL_OPTION_CLUSTER.fullmatch(value):
            # the letters after the first `o`/`O` are the option name (`-oglobsubst`); with none, the next word is
            attached = value[re.search(r"[oO]", value[1:]).start() + 2 :]
            name = [attached] if attached else operands[index + 1 : index + 2]
            if name not in ([safe] for safe in _SAFE_SHELL_OPTIONS):
                return True
            index += 1 if attached else 2
        else:
            index += 1
    return False


def _unmodeled_construct(text: str) -> bool:
    """Report whether shell code holds any construct outside the grammar the reader models.

    Examples:
        >>> _unmodeled_construct('x="${TMPDIR:-/tmp}/run-$((n + 1))"; cat <<\\'EOF\\'\\nPATH = 0\\nEOF')
        False
        >>> _unmodeled_construct("ls *(N)(e:id:)")
        True
        >>> _unmodeled_construct("cat << \\\\EOF\\nx\\nEOF")
        True
        >>> _unmodeled_construct("echo ${ hash -p /tmp/x git; }")
        True
        >>> _unmodeled_construct("cat {IFS}</dev/null f"), _unmodeled_construct("cat 2>/dev/null f")
        (True, False)
    """
    return _LiveScan(text).refused()


def _unmodeled_substitution(body: str) -> bool:
    """Report whether the body of a ``$(…)``, backtick or ``<(…)`` substitution leaves the modeled grammar.

    The body is judged as shell code, and a comment that runs to its very end is refused too: the shell would have
    read the ``)`` the reader took as the close as comment text, so the substitution goes on past it.

    Examples:
        >>> _unmodeled_substitution("date # stamp\\n")
        False
        >>> _unmodeled_substitution(": # ")
        True
    """
    return _LiveScan(body, substitution=True).refused()


class _LiveScan:
    """Walk shell text as the shell expands it, refusing every construct outside the modeled grammar.

    Only live text is judged: the top level, ``"…"``, substitution bodies and unquoted heredoc bodies. Single-quoted,
    ``$'…'`` and escaped text and quoted heredoc bodies are data, so a quoted message or a ``<<'PY'`` body never
    trips a rule. Admitted: ``${…}`` in the forms :func:`_parameter_unmodeled` accepts, ``$((…))`` in the forms
    :func:`_arithmetic_unmodeled` accepts, ``$(…)``/backticks/``<(…)``/``>(…)``/``=(…)`` whose bodies pass in turn,
    ``<<<``, a heredoc opened in a :data:`_HEREDOC_OPENING` form that reaches its delimiter, and ``(`` attached to a
    word only as ``NAME=(``, ``=(`` or ``name()``. Refused: anything else, including ``((…))``, ``$[…]``, ``$~x``, a
    zsh ``$name[…]`` subscript that can assign, every other attached ``(`` (zsh glob qualifiers and extended globs),
    a comment holding ``<<``, and a ``{name}`` descriptor prefix on a redirection (:data:`_FD_VARIABLE_PREFIX`).

    Args:
        text: The text to walk.
        mode: ``"code"`` for shell code; ``"operand"`` for the operand of a ``${…}`` expansion, where quotes apply
            but heredoc, comment and parenthesis rules do not; ``"body"`` for an unquoted heredoc body, where only
            ``$``, backticks and backslashes are special.
        substitution: True when ``text`` is the body of a substitution, where a comment must end before the body
            does (:func:`_unmodeled_substitution`).
    """

    def __init__(self, text: str, mode: str = "code", substitution: bool = False) -> None:
        self.text = text
        self.mode = mode
        self.substitution = substitution
        self.index = 0
        self.word_start = 0
        self.double = False
        self.pending: list[tuple[str, bool, bool]] = []
        # the simple command read so far, for telling a grouping `(` from one that opens a glob word: its words as
        # written, the grouping parentheses still open, whether a `)` just closed one, and whether the next word is
        # a redirection target
        self.words: list[str] = []
        self.groups = 0
        self.closed = False
        self.redirect = False

    def refused(self) -> bool:
        """Return True when the walk meets an unmodeled construct or a heredoc that never ends."""
        while self.index < len(self.text):
            verdict = self._quoting()
            if verdict is None:
                verdict = self._expansion()
            if verdict is None and self.mode == "code" and not self.double:
                verdict = self._syntax()
            if verdict is None:
                self.index += 1
            elif verdict:
                return True
        return bool(self.pending)

    def _quoting(self) -> bool | None:
        """Pass a backslash escape, a quoted span or a ``"`` toggle; None when none starts here."""
        text, index = self.text, self.index
        if text[index] == "\\":
            self.index += 2
            return False
        if self.mode == "body":
            return None
        if text[index] == '"':
            self.double = not self.double
            self.index += 1
            return False
        if self.double:
            return None
        if text[index] == "'":
            close = text.find("'", index + 1)
            self.index = close + 1
            return close < 0
        body = _ansi_c_body(text, index)
        if body is None:
            return None
        self.index = _read_ansi_c(text, body)[1]
        return False

    def _expansion(self) -> bool | None:
        """Judge an expansion opening here; None when none does."""
        text, index = self.text, self.index
        if text.startswith("$((", index):
            read = _read_closed(text, index + 3, "(", ")")
            if read is None or text[read[1] : read[1] + 1] != ")":
                return True
            self.index = read[1] + 1
            return _arithmetic_unmodeled(read[0])
        readers = (("$(", "(", ")", _unmodeled_substitution), ("${", "{", "}", _parameter_unmodeled))
        for opener, open_char, close_char, judge in (*readers, ("`", None, "`", _unmodeled_substitution)):
            if text.startswith(opener, index):
                read = _read_closed(text, index + len(opener), open_char, close_char)
                if read is None:
                    return True
                self.index = read[1]
                return judge(read[0])
        if text[index] == "$" and not (
            _DOLLAR_FOLLOW.match(text, index + 1)
            or ((self.double or self.mode == "body") and text[index + 1 : index + 2] in ("'", '"'))
        ):
            # admit set: zsh `$~x` `$=x` `$^x`, `$[…]` and any other form opened by `$` are refused
            return True
        subscript = _ZSH_SUBSCRIPTED.match(text, index)
        if subscript is None:
            return None
        value = subscript.group(1)
        return True if not _SUBSCRIPT_TEXT.fullmatch(value) or _SUBSCRIPT_REFUSED.search(value) else None

    def _syntax(self) -> bool | None:
        """Judge unquoted shell syntax: comments, heredocs, process substitution, parentheses, word boundaries."""
        text, index = self.text, self.index
        char = text[index]
        if char == "#" and index == self.word_start:
            # right after a grouping `(` or a `|` bash reads a comment, but zsh may read `#` inside a pattern
            # (`[[ x == (#i)y ]]`, `(a|#b)`) as a glob flag, so where the comment ends is unsettled
            if text[index - 1 : index] in ("(", "|"):
                return True
            end = text.find("\n", index)
            if end < 0 and self.substitution:
                return True
            end = len(text) if end < 0 else end
            self.index = self.word_start = end
            return "<<" in text[index:end]
        if char == "\n" and self.pending:
            return self._heredoc_bodies()
        if (char in "<>" or text.startswith("&>", index)) and _FD_VARIABLE_PREFIX.fullmatch(
            text[self.word_start : index]
        ):
            # `{name}>` allocates a descriptor and assigns its number to `name`; only a digit descriptor is modeled
            return True
        if text.startswith("<<<", index):
            self._end_word(index)
            self.redirect = True
            self.index = self.word_start = index + 3
            return False
        if text.startswith("<<", index):
            return self._heredoc_opening()
        if char in "<>" and text[index + 1 : index + 2] == "(":
            return self._substitution(index + 2)
        if char == "(":
            return self._parenthesis()
        # only blanks and newlines separate words: `\r`, `\v` and `\f` are word characters to the shell
        if char in " \t\n;&|<>)":
            self._end_word(index)
            self._separator(index)
            self.word_start = index + 1
        return None

    def _end_word(self, index: int) -> None:
        """Record the word that ends at ``index``, if one does; a redirection's target is then consumed."""
        if index > self.word_start:
            self.words.append(self.text[self.word_start : index])
            self.redirect = False

    def _separator(self, index: int) -> None:
        """Update the simple-command state at a word-ending character other than a blank."""
        text, char = self.text, self.text[index]
        if (
            char in "<>"
            or (char == "&" and _is_redirection_ampersand(text, index))
            or (char == "|" and text[index - 1 : index] == ">")
        ):
            self.redirect = True
        elif char in ";&|\n":
            self.words, self.closed, self.redirect = [], False, False
        elif char == ")":
            # a `)` closing a grouping ends a command; one with none open closes a `case` pattern, after which the
            # arm's command starts
            self.words, self.redirect = [], False
            self.closed = self.groups > 0
            self.groups = max(self.groups - 1, 0)

    def _heredoc_opening(self) -> bool:
        """Queue a heredoc opened in a modeled form; refuse any other ``<<``."""
        match = _HEREDOC_OPENING.match(self.text, self.index)
        if match is None:
            return True
        self._end_word(self.index)
        delimiter = match.group(2) or match.group(3) or match.group(4)
        self.pending.append((delimiter, match.group(2) is None, match.group(1) == "-"))
        self.index = self.word_start = match.end()
        return False

    def _heredoc_bodies(self) -> bool:
        """Read every queued heredoc body after a newline; judge the unquoted ones."""
        self.index += 1
        for delimiter, quoted, strips_tabs in self.pending:
            body = self._heredoc_body(delimiter, strips_tabs)
            if body is None or (not quoted and _LiveScan(body, "body").refused()):
                return True
        self.pending = []
        self.word_start = self.index
        self.words, self.closed, self.redirect = [], False, False
        return False

    def _heredoc_body(self, delimiter: str, strips_tabs: bool) -> str | None:
        """Read lines up to the delimiter line; None when the text ends first."""
        lines: list[str] = []
        while self.index < len(self.text):
            end = self.text.find("\n", self.index)
            end = len(self.text) if end < 0 else end
            line = self.text[self.index : end]
            self.index = end + 1
            if (line.lstrip("\t") if strips_tabs else line) == delimiter:
                return "\n".join(lines)
            lines.append(line)
        return None

    def _parenthesis(self) -> bool:
        """Judge an unquoted ``(``: grouping, ``=(``, an array assignment, a function definition, or refused."""
        word = self.text[self.word_start : self.index]
        if not word:
            if self.text.startswith("((", self.index) or not self._opens_group():
                return True
            self.groups += 1
            self.words, self.closed = [], False
            self.index = self.word_start = self.index + 1
            return False
        if word == "=" or _ARRAY_ASSIGNMENT_OPEN.fullmatch(word):
            return self._substitution(self.index + 1)
        if self.text[self.index + 1 : self.index + 2] == ")" and _FUNCTION_WORD.fullmatch(word):
            self.index = self.word_start = self.index + 2
            return False
        return True

    def _opens_group(self) -> bool:
        """Report whether a ``(`` opening a word here groups rather than starting a pattern word.

        It groups at command position (segment start, after a separator, another grouping ``(``, a ``case`` pattern's
        ``)`` or a reserved word such as ``if`` or ``!``), inside ``[[ … ]]``, as a ``case`` pattern right after ``in``,
        and as the ``()`` of a function definition. Anywhere else, after a command word, a redirection operator or a
        group just closed, zsh reads ``(`` as the start of a glob word, whose glued second group (``(*)(e:…:)``) is a
        qualifier that runs code.
        """
        if self.text[self.index + 1 : self.index + 2] == ")":
            return True
        if self.redirect or self.closed:
            return False
        words = self.words
        return (
            all(word in _GROUP_OPENERS for word in words)
            or (words[:1] == ["[["] and "]]" not in words)
            or (len(words) == 3 and words[0] == "case" and words[2] == "in")
        )

    def _substitution(self, start: int) -> bool:
        """Judge the ``(…)`` body starting at ``start`` as shell code."""
        read = _read_closed(self.text, start, "(", ")")
        if read is None:
            return True
        self.index = read[1]
        return _unmodeled_substitution(read[0])


def _read_closed(text: str, start: int, open_char: str | None, close_char: str) -> tuple[str, int] | None:
    """Read a delimited run like :func:`_read_delimited`, or return None when the text ends before it closes.

    Examples:
        >>> _read_closed("$(a) b", 2, "(", ")")
        ('a', 4)
        >>> _read_closed("$(a (b)", 2, "(", ")") is None
        True
    """
    inner, end = _read_delimited(text, start, open_char, close_char)
    return (inner, end) if len(inner) == end - start - 1 else None


def _parameter_unmodeled(inner: str) -> bool:
    """Report whether the body of a ``${…}`` expansion leaves the forms the reader models.

    Admitted: a parameter (:data:`_PARAMETER_NAME`), its length (``${#x}``), a digit offset (``${x:0:8}``), or one
    reading operator (:data:`_PARAMETER_OPERATOR`) with an operand that passes the grammar scan and, quotes and
    escapes resolved, stores no code opener. Every assigning, indirect, flagged or command form is refused.

    Examples:
        >>> _parameter_unmodeled("TMPDIR:-/tmp")
        False
        >>> _parameter_unmodeled("f##*/")
        False
        >>> _parameter_unmodeled("#items[@]")
        False
        >>> _parameter_unmodeled("x:0:8")
        False
        >>> _parameter_unmodeled("PATH:=/tmp")
        True
        >>> _parameter_unmodeled("(e)x")
        True
        >>> _parameter_unmodeled("a[PATH=1]")
        True
        >>> _parameter_unmodeled("a:-a[\\\\$\\\\(id\\\\)0]")
        True
    """
    if inner == "#" or _PARAMETER_LENGTH.fullmatch(inner):
        return False
    name = _PARAMETER_NAME.match(inner)
    if name is None:
        return True
    rest = inner[name.end() :]
    if not rest or _PARAMETER_OFFSET.fullmatch(rest):
        return False
    operator = _PARAMETER_OPERATOR.match(rest)
    if operator is None:
        return True
    operand = rest[operator.end() :]
    stored = _decode_operand(_mask_live_expansions(operand))
    return _LiveScan(operand, "operand").refused() or bool(_CODE_OPENER.search(stored))


def _decode_operand(text: str) -> str:
    """Return text with backslash escapes and ``'…'``/``"…"``/``$'…'`` quotes resolved, as the shell stores it.

    Examples:
        >>> _decode_operand("a\\\\$'b c'\\"d\\"")
        'a$b cd'
        >>> _decode_operand("$'\\\\x24\\\\x28id\\\\x29'")
        '$(id)'
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        body = _ansi_c_body(text, index)
        if body is not None:
            decoded, index = _read_ansi_c(text, body)
            out.append(decoded)
        elif char == "\\":
            out.append(text[index + 1 : index + 2])
            index += 2
        elif char in "'\"":
            close = text.find(char, index + 1)
            close = len(text) if close < 0 else close
            out.append(text[index + 1 : close])
            index = close + 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _arithmetic_unmodeled(expr: str) -> bool:
    """Report whether the text of ``$((…))`` does more than read values and compare them.

    Substitutions inside are judged as code and stand for a number. What remains must alternate operands (numbers,
    names other than lookup variables, admitted ``${…}``) and operators (:data:`_ARITHMETIC_BINARY`), with unary
    ``!``/``-``/``+`` and balanced parentheses; :data:`_ARITHMETIC_REFUSED` text is refused outright.

    Examples:
        >>> _arithmetic_unmodeled(" n + 1 ")
        False
        >>> _arithmetic_unmodeled("COUNT < 5 ? COUNT : 5")
        False
        >>> _arithmetic_unmodeled(" $(wc -l < f) + ${#x} ")
        False
        >>> _arithmetic_unmodeled("PATH=0")
        True
        >>> _arithmetic_unmodeled("n << 2")
        True
        >>> _arithmetic_unmodeled("cd x && ls")
        True
    """
    masked = _mask_arithmetic_operands(expr)
    if masked is None or _ARITHMETIC_REFUSED.search(masked):
        return True
    expect_operand, depth, index = True, 0, 0
    while index < len(masked):
        char = masked[index]
        operand = _ARITHMETIC_OPERAND.match(masked, index) if expect_operand else None
        binary = None if expect_operand else _ARITHMETIC_BINARY.match(masked, index)
        if char in " \t" or (expect_operand and char in "!-+"):
            index += 1
        elif char == "(" and expect_operand:
            depth, index = depth + 1, index + 1
        elif char == ")" and not expect_operand and depth:
            depth, index = depth - 1, index + 1
        elif operand is not None and not _LOOKUP_ARITHMETIC_NAME.fullmatch(operand.group()):
            expect_operand, index = False, operand.end()
        elif binary is not None:
            expect_operand, index = True, binary.end()
        else:
            return True
    return expect_operand or depth != 0


def _mask_arithmetic_operands(expr: str) -> str | None:
    """Return arithmetic text with each substitution and ``${…}`` replaced by ``0``; None when one is refused.

    Examples:
        >>> _mask_arithmetic_operands("$(wc -l < f) + ${N:-1}")
        '0 + 0'
        >>> _mask_arithmetic_operands("${PATH:=1}") is None
        True
    """
    out: list[str] = []
    index = 0
    while index < len(expr):
        opener = next((o for o in ("$(", "${", "`") if expr.startswith(o, index)), None)
        if opener is None:
            out.append(expr[index])
            index += 1
            continue
        open_char, close_char = {"$(": ("(", ")"), "${": ("{", "}"), "`": (None, "`")}[opener]
        read = _read_closed(expr, index + len(opener), open_char, close_char)
        judge = _arithmetic_parameter_unmodeled if opener == "${" else _unmodeled_substitution
        if read is None or judge(read[0]):
            return None
        out.append("0")
        index = read[1]
    return "".join(out)


def _arithmetic_parameter_unmodeled(inner: str) -> bool:
    """Report whether a ``${…}`` inside ``$((…))`` may expand to arithmetic that does more than read a value.

    Arithmetic evaluates the text an expansion yields, so an operand it may expand to (``${x:-PATH=1}``) must itself
    be a number or a name other than a lookup variable once its quotes and escapes are resolved.

    Examples:
        >>> _arithmetic_parameter_unmodeled("BATCH_SIZE:-1")
        False
        >>> _arithmetic_parameter_unmodeled("#desc")
        False
        >>> _arithmetic_parameter_unmodeled("x:-PATH=1")
        True
        >>> _arithmetic_parameter_unmodeled("x:-++n")
        True
    """
    if _parameter_unmodeled(inner):
        return True
    operand = _parameter_operand(inner)
    if operand is None:
        return False
    value = _decode_operand(operand)
    return not _ARITHMETIC_OPERAND.fullmatch(value) or bool(_LOOKUP_ARITHMETIC_NAME.fullmatch(value))


def _parameter_operand(inner: str) -> str | None:
    """Return the operand after the reading operator of a ``${…}`` body, or None when it has no operator.

    Examples:
        >>> _parameter_operand("x:-1")
        '1'
        >>> _parameter_operand("x:0:8") is None
        True
    """
    name = _PARAMETER_NAME.match(inner)
    operator = _PARAMETER_OPERATOR.match(inner, name.end()) if name else None
    return inner[operator.end() :] if operator else None


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


#: One ``case`` pattern: unquoted characters other than space, parentheses and ``|``, escapes, or quoted runs.
#: Whitespace is spelled out as ASCII, not ``\s``, so ``hooks/blueprint-allow.js`` can match it exactly.
_CASE_PATTERN = r"""(?:[^ \t\n\r\f\v()'"|\\]|\\.|"[^"]*"|'[^']*')+"""
#: A ``case`` arm label at the start of a segment: one pattern closed by ``)``, optionally opened by ``(``.
_CASE_LABEL = re.compile(rf"[ \t\n\r\f\v]*\(?{_CASE_PATTERN}\)")
#: A segment that is one bare ``case`` pattern, an alternative before ``|`` (``a`` in ``a|b)``).
_CASE_ALTERNATIVE = re.compile(rf"[ \t\n\r\f\v]*\(?{_CASE_PATTERN}[ \t\n\r\f\v]*")
#: A shell function name as written in a definition.
_FUNCTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
#: A heredoc operator: ``<<`` or ``<<-``, then the delimiter, optionally quoted; never the ``<<<`` here-string.
_HEREDOC_OPERATOR = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
#: A word that is exactly one plain variable expansion, ``$NAME`` or ``${NAME}``.
_VARIABLE_WORD = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
#: ``case`` and ``esac`` keywords, counted to tell a pattern's ``)`` from the end of a ``$(...)`` body.
_CASE_KEYWORD = re.compile(r"(?<![\w$-])case(?=[ \t])")
_ESAC_KEYWORD = re.compile(r"(?<![\w$-])esac(?![\w-])")


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


def _case_line_arm(text: str) -> str | None:
    """Return the command text of a ``case`` arm written on the ``case … in`` line itself, or None.

    Read on shell words, as written: ``case`` and ``in`` count only unquoted, and the subject is one whole word, so an
    ``in`` inside a quoted subject (``case 'x in y' in``) never ends it.

    Examples:
        >>> _case_line_arm('case "$X" in a) mv x y')
        ' mv x y'
        >>> _case_line_arm("case 'x in y' in *) eval x")
        ' eval x'
        >>> _case_line_arm('case "$X" in') is None
        True
        >>> _case_line_arm('case "$X" in a') is None
        True
        >>> _case_line_arm("echo a) b") is None
        True
    """
    spans = _shell_word_spans(text)
    start = _command_start_index([word for word, _ in spans])
    if len(spans) < start + 3 or spans[start][0].raw != "case" or spans[start + 2][0].raw != "in":
        return None
    after = text[spans[start + 2][1] :]
    end = _case_label_end(after)
    return after[end:] if end is not None else None


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
        >>> _heredoc_operators('grep <<< "word"')
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
    body: list[str] = []
    for line in command.split("\n"):
        if pending:
            delimiter, quoted, strips_tabs = pending[0]
            if (line.lstrip("\t") if strips_tabs else line) != delimiter:
                body.append(line)
                continue
            pending.pop(0)
            # a substitution may span body lines, so the body is read whole once its delimiter is reached
            if not quoted:
                live.extend(_heredoc_substitutions("\n".join(body)))
            body = []
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
