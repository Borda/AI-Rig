#!/usr/bin/env python
"""consolidate_inventory.py — inventory instruction sections and vet merge clusters, for /foundry:audit --consolidate.

Four subcommands, all read-only toward the instruction sources (only ``--out``, ``--review-inputs`` and the
obligation files beside the proposals are written):

``inventory``
    Splits every instruction file of the plugin source tree into heading-level sections and records, per section,
    its estimated tokens, how it is loaded (``always`` / ``scoped`` / ``lazy``, with the exact ``paths:`` globs and
    the trigger that loads it), whether it is the stub or the ``_full`` side of a progressive-disclosure pair, and
    the dependencies that make a merge unsafe: membership in the byte-identical shared-file MANIFEST
    (``propagate_shared.py``), ``policy-sibling`` markers and whether their targets resolve, YAML frontmatter that
    carries routing or scope metadata, and the other files that cite the section as ``<file> §<Heading>``. A section
    with an unresolved dependency is ``blocked`` with the reason.

``select``
    Applies the run scope (rule/skill/agent kinds, plugin names, skill names), the token floor and the group cap to
    the inventory's ranked groups, and splits the chosen groups into balanced batches. Everything not chosen is
    reported with the reason, so a run states what it did not examine.

``validate``
    Takes the inventory and a clusters file (``[{"id": ..., "items": [<item id>, ...]}]``) and reports per cluster
    every structural reason it cannot be merged: unknown, duplicate, too few or already claimed members, blocked
    members, a single file (Check 29 territory), mixed load scopes, stub/``_full`` pair flattening, an unsafe id.
    ``merge_allowed`` is false for lazy groups (a skill's or the agents' files never co-load unconditionally), so
    those clusters can only be sharpened or kept.

``settle``
    Takes the validation, the judge decisions and, once available, the independent reviews, and applies the rules
    that must not depend on a model remembering them: downgrade a merge that is not allowed, not shorter, missing
    its proposal or obligation map, or that deletes a section other files cite; price validated savings per load
    class; write each remaining proposal's obligation map beside it; and write the inputs the reviewer needs.

Token estimate is ``bytes / 3``, the convention in ``plugins/CLAUDE.md`` §Length Unit Convention.

Usage:
    consolidate_inventory.py inventory [--root <repo>] [--kinds rules,skills,agents] [--out <file>]
    consolidate_inventory.py select --inventory <file> [--scope "<tokens>"] [--min-tokens N] [--max-groups N]
        [--batches N] [--out <file>]
    consolidate_inventory.py validate --inventory <file> --clusters <file> [--min-cluster-tokens N]
        [--max-judged N] [--out <file>]
    consolidate_inventory.py settle --vetted <file> --judgments <file> --proposals-dir <dir> [--reviews <file>]
        [--review-inputs <file>] [--out <file>]

Exit codes:
    0   a completed run — findings are in the output, not the status
    1   an input file is missing, is not valid JSON, or has the wrong shape
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import posixpath
import re
import sys
import tempfile
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from itertools import pairwise
from pathlib import Path

from propagate_shared import MANIFEST

#: Inventory schema version written to the output; ``select`` and ``validate`` refuse any other.
SCHEMA_VERSION = 1

#: Instruction kinds the inventory can cover.
ALL_KINDS = ("rules", "skills", "agents")

#: ``paths:`` globs that match every file, so a rule carrying only these loads like an unscoped one.
ALWAYS_GLOBS = frozenset({"**", "**/*", "*"})

#: Full-body stems whose stub is not ``rules/<stem>.md``, mapped to the stub's plugin-relative path.
PAIR_STUBS = {"notebook-style": "rules/notebooks.md", "CLAUDE-full": "CLAUDE.src.md"}

#: Top-level folders of a plugin that hold other components, so they are not fragments of a skill kept at the root.
_NOT_SKILL_DIRS = frozenset(
    {"skills", "agents", "commands", "hooks", "tests", "bin", "scripts", "docs", "node_modules"}
)

#: Root-level Markdown that Claude does not load as skill text.
_NOT_SKILL_FILES = frozenset({"CLAUDE.md", "AGENTS.md"})

#: Skill-directory Markdown that documents the skill and is never loaded as an instruction.
SKILL_DOC_NAMES = frozenset({"ARCH.md", "README.md", "CHANGELOG.md"})

#: Suffix of the foundry-present variant of a rule that a plugin also ships in full (delivered one or the other).
ALT_SUFFIX = "-delta"

#: Smallest cluster, in estimated tokens, worth a judge: a merge cannot save more than the cluster holds.
MIN_CLUSTER_TOKENS = 400

#: Most clusters sent to a judge in one run; the rest are listed with the reason, never silently dropped.
MAX_JUDGED = 20

#: Scope tokens that name every plugin, as in the audit skill's scope grammar.
ALL_PLUGINS_TOKEN = frozenset({"plugins"})

#: Fields every entry of a merge proposal's obligation map must carry as non-empty text.
OBLIGATION_FIELDS = ("original", "from", "to")

#: Decisions a judge may return.
DECISIONS = ("keep", "sharpen", "merge-prune")

#: A cluster id becomes a file name, so it stays inside this alphabet.
_CLUSTER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

#: One item of an inline YAML list: a quoted string (commas allowed inside) or a bare word.
_INLINE_ITEM = re.compile(r"'(?:[^']|'')*'|\"[^\"]*\"|[^, \t][^,]*")

#: A ``<!-- policy-sibling: ... -->`` marker, capturing its payload.
_SIBLING = re.compile(r"<!--\s*policy-sibling:\s*(.*?)\s*-->", re.DOTALL)

#: A repo-relative path inside a sibling payload (up to whitespace, comma, parenthesis or section sign).
_SIBLING_PATH = re.compile(r"(?:plugins|tests|\.claude|\.codex)/[^\s,()§]+")

#: A Markdown ATX heading line (up to three spaces of indent): its hashes and the title text, closing hashes included.
_ATX_LINE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*))?$")

#: The closing sequence of an ATX heading: trailing hashes set off by a space, or the whole title.
_ATX_CLOSE = re.compile(r"(?:^|[ \t]+)#+$")

#: The underline of a setext heading: a run of ``=`` (level 1) or ``-`` (level 2).
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")

#: A blockquote marker, a thematic break, and a list-item marker (indent, marker, spaces after it, the item's text).
_QUOTE = re.compile(r"^ {0,3}>")
_THEMATIC = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_LIST_MARKER = re.compile(r"^( {0,3})([-+*]|\d{1,9}[.)])( *)(.*)$")

#: The head of a link reference definition: a label (brackets only escaped), a colon, and a destination (``<...>`` or
#: a run without spaces or control characters) after spaces and at most one line ending.
_DEF_HEAD = re.compile(
    r"\[((?:[^\[\]\\]|\\[\s\S])+)\]:[ \t]*\n?[ \t]*(<(?:[^<>\n\\]|\\[^\n])*>|(?!<)[^\x00-\x20\x7f]+)"
)

#: The optional title of a definition: set off by white space (one line ending allowed), in quotes or parentheses, and
#: followed by nothing but spaces to the end of its line.
_DEF_TITLE = re.compile(
    r"[ \t]*\n?[ \t]*(?<=\s)(?:\"(?:[^\"\\]|\\[\s\S])*\"|'(?:[^'\\]|\\[\s\S])*'|\((?:[^()\\]|\\[\s\S])*\))[ \t]*(?=\n|$)"
)

#: What may follow the destination when no title does: spaces to the end of the line.
_DEF_EOL = re.compile(r"[ \t]*(?=\n|$)")

#: An HTML comment on one line, which a heading title drops.
_INLINE_COMMENT = re.compile(r"<!--.*?-->")

#: The deepest heading level that is a section of its own; level-5 and level-6 headings belong to their section.
_SECTION_LEVEL = 4

#: A code-fence line (indent of at most three spaces; a backtick fence has no backtick after its marker), capturing the
#: marker run. A deeper indent is an indented code block, not a fence.
_FENCE = re.compile(r"^ {0,3}(`{3,}(?=[^`]*$)|~{3,})")

#: HTML block starts that end on a given closing line: script/pre/style/textarea, processing instruction, declaration,
#: CDATA, comment (CommonMark types 1 to 5).
_HTML_UNTIL = (
    (re.compile(r"^ {0,3}<!--"), re.compile(r"-->")),
    (
        re.compile(r"^ {0,3}<(?:script|pre|style|textarea)(?:\s|>|$)", re.IGNORECASE),
        re.compile(r"</(?:script|pre|style|textarea)>", re.IGNORECASE),
    ),
    (re.compile(r"^ {0,3}<\?"), re.compile(r"\?>")),
    (re.compile(r"^ {0,3}<!\[CDATA\["), re.compile(r"\]\]>")),
    (re.compile(r"^ {0,3}<![A-Za-z]"), re.compile(r">")),
)

#: HTML block starts that end at the next blank line: block-level tag names (type 6), and a complete tag alone on its
#: line that cannot interrupt a paragraph (type 7).
_HTML_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|"
    "fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|"
    "menuitem|nav|noframes|ol|optgroup|option|p|param|section|source|summary|table|tbody|td|tfoot|th|thead|title|tr|"
    "track|ul"
)
_HTML_TO_BLANK = re.compile(rf"^ {{0,3}}</?(?:{_HTML_BLOCK_TAGS})(?:\s|/?>|$)", re.IGNORECASE)
_HTML_TAG_ALONE = re.compile(r"^ {0,3}(?:<[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>|</[A-Za-z][A-Za-z0-9-]*\s*>)[ \t]*$")

#: A ``<file>.md §<Heading>`` or ``<file>.md (§<Heading>)`` citation, capturing the file name and the text from the
#: first heading on to the next file.
_CITATION = re.compile(
    r"([\w./\\-]+\.md)[`'\"\])]*\s*\(?\s*§\s*([`'\"*_\u201c\u2018]*)((?:(?![\w./\\-]+\.md[`'\"\])]*\s*\(?\s*§)[^;|\n])+)"
)

#: Quote or emphasis marks that close a quoted heading: followed by a space, a separator or the end of the text.
_CLOSING_MARKS = re.compile(r"[\"'*_\u201d\u2019]+(?=\s|[,;.)\]]|$)")

#: One cited heading inside the text a citation captured: after a section sign, up to the next backtick or section sign.
#: Group 1 is the opening quote or emphasis marks (a quoted title drops its closing marks), group 2 the text.
_SECTION_ITEM = re.compile(r"§\s*([`'\"*_\u201c\u2018]*)([^§;|\n]+)")


@dataclass
class Load:
    """How an instruction section reaches the model."""

    hierarchy: str
    trigger: str
    paths: list[str] = field(default_factory=list)
    side: str = ""
    pair: str = ""
    owner: str = ""


@dataclass
class Item:
    """One heading-level instruction section and the facts that decide whether it may be merged."""

    id: str
    file: str
    section: str
    start_line: int
    end_line: int
    tokens: int
    kind: str
    load: Load
    plugin: str = ""
    group: str = ""
    inbound: list[str] = field(default_factory=list)
    shared: str = ""
    siblings: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    guarded: list[dict] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    """Estimate tokens as UTF-8 bytes divided by three."""
    return len(text.encode("utf-8")) // 3


#: Stand-in glob for a ``paths:`` key whose value could not be read, so the rule is never taken for an unscoped one.
UNPARSED_PATHS = "<unparsed paths:>"


#: A ``paths`` key line in YAML frontmatter, bare or quoted, with the text after its colon.
_PATHS_KEY = re.compile(r"^paths[ \t]*:(.*)$")

#: A top-level frontmatter key: a bare identifier, then a colon and a space or the end of the line.
_PLAIN_KEY = re.compile(r"^[A-Za-z_][\w-]*[ \t]*:(?:[ \t]|$)")

#: A ``paths`` key at the start of a line (any indent) or after ``{`` or ``,`` in a flow mapping.
_ANY_PATHS_KEY = re.compile(r"""(?:^|[{,])[ \t]*(?:paths|"paths"|'paths')[ \t]*:""", re.MULTILINE)

#: A scalar's first character when it starts a construct this reader does not read (block scalar, anchor, tag, ...).
_UNREAD_START = frozenset("|>&*!%@`{}?[],")


#: A character YAML either breaks a line at (NEL, line and paragraph separator) or refuses outright (a control character
#: other than tab, a non-character): a frontmatter holding one is not read.
_YAML_UNREAD_CHAR = re.compile(r"[^\n\r\x20-\x7e\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]|[\t\x85\u2028\u2029]")

#: Plain scalars YAML reads as something other than a string: null, booleans, numbers and dates.
_TYPED_PLAIN = re.compile(
    r"^(?:~|null|true|false|yes|no|on|off|y|n|<<|=|[-+]?\.(?:inf|nan)|[-+]?(?=[\d_.]*\d)[\d_]*\.?[\d_]*(?:e[-+]?\d+)?"
    r"|[-+]?0b[01_]+|[-+]?0x[0-9a-f_]+|[-+]?0o?[0-7_]+|[-+]?\d[\d_]*(?::[0-5]?\d)+(?:\.\d*)?"
    r"|\d{4}-\d\d?-\d\d?(?:[t ].*)?)$",
    re.IGNORECASE,
)


def _scalar(raw: str) -> str | None:
    """Return the text of one YAML scalar (plain, 'single' or "double" quoted), or None when it is not one this reads.

    Anything beyond a closing quote other than a comment, a backslash escape, a colon followed by a space (a mapping),
    or an indicator that opens a block scalar, anchor, alias, tag or flow map is refused, so the caller fails closed.
    """
    text = raw.strip(" \t")
    if not text or text[0] in _UNREAD_START:
        return None
    if text[0] in "'\"":
        end = text.find(text[0], 1)
        rest = text[end + 1 :].strip(" \t") if end > 0 else "x"
        return text[1:end] if end > 1 and "\\" not in text[:end] and (not rest or rest.startswith("#")) else None
    plain = re.split(r"[ \t]#", text, maxsplit=1)[0].strip(" \t")
    return plain if _plain_glob(plain) else None


def _plain_glob(plain: str) -> bool:
    """Report whether a plain scalar is a string this reader takes as a glob (no comment, mapping or YAML type)."""
    return (
        bool(plain)
        and plain[0] != "#"
        and not re.search(r":(?:[ \t]|$)", plain)
        and not re.match(r"[-?:](?:[ \t]|$)", plain)
        and not _TYPED_PLAIN.match(plain)
    )


def _flow_close(text: str) -> int:
    """Return the index of the ``]`` that closes the flow list opening ``text``, or -1 when there is none to read.

    A quote opens only where an item starts (after ``[`` or ``,``), so ``']'`` inside a quoted item is not the end; a
    nested ``[`` or a ``{`` is a construct this reader does not read.
    """
    quote = ""
    start = True
    for pos, char in enumerate(text[1:], 1):
        if quote:
            quote = "" if char == quote else quote
        elif char == "]":
            return pos
        elif char in "[{":
            return -1
        elif start and char in "'\"":
            quote = char
            start = False
        elif char == ",":
            start = True
        elif char not in " \t":
            start = False
    return -1


def _strip_flow_comments(lines: list[str]) -> list[str]:
    """Return the physical lines of a flow list without their ``#`` comments (a ``#`` after a space, outside quotes).

    A quoted item may run over several lines, and a ``#`` inside it is text even at the start of a continuation line, so
    the quote state is carried from one line to the next.
    """
    quote = ""
    start = True
    kept: list[str] = []
    for line in lines:
        cut = len(line)
        for pos, char in enumerate(line):
            if quote:
                quote = "" if char == quote else quote
            elif char == "#" and (pos == 0 or line[pos - 1] in " \t"):
                cut = pos
                break
            elif start and char in "'\"":
                quote = char
                start = False
            elif char in "[,":
                start = True
            elif char not in " \t":
                start = False
        kept.append(line[:cut] + ("\x00" if cut < len(line) else ""))
    return kept


def _flow_list(text: str) -> list[str] | None:
    """Return the items of a flow list that opens the text and closes it (nothing but spaces after), else None."""
    close = _flow_close(text) if text.startswith("[") else -1
    if close < 0 or text[close + 1 :].strip(" \t\x00"):
        return None
    inner = text[1:close]

    def skip(at: int) -> int:
        while at < len(inner) and inner[at] in " \t\x00":
            at += 1
        return at

    items: list[str | None] = []
    pos = skip(0)
    while pos < len(inner):
        piece = _INLINE_ITEM.match(inner, pos)
        if piece is None:
            return None
        text_of = piece.group(0).rstrip(" \t\x00")
        if "\x00" in text_of or (text_of[0] not in "'\"" and re.search(r"[:?{}\[\]]", text_of)):
            return None
        items.append(_scalar(text_of))
        pos = skip(piece.end())
        if pos < len(inner):
            if inner[pos] != ",":
                return None
            pos = skip(pos + 1)
    return None if not items or any(item is None for item in items) else [i for i in items if i]


def _block_list(lines: list[str]) -> list[str] | None:
    """Return the items of a block list (``- glob`` lines), or None when any line is not an item this reads.

    Every item starts at the first item's indentation; a deeper line is the continuation of the item above it (YAML
    folds it into one scalar), which this reader does not follow.
    """
    content = [line for line in lines if line.strip(" \t")]
    if not content:
        return None
    indent = len(content[0]) - len(content[0].lstrip(" "))
    items = [_scalar(line.strip(" \t")[1:]) if re.match(rf"^ {{{indent}}}-[ \t]", line) else None for line in content]
    return None if any(item is None for item in items) else [i for i in items if i]


def _paths_value(first: str, rest: list[str]) -> list[str]:
    """Return the globs of a ``paths:`` value: text on the key's line plus the lines indented below it.

    Only three shapes are read: a block list, a flow list (also continued on the following lines), and one scalar.
    Every other YAML spelling is returned as ``UNPARSED_PATHS``, never guessed at.
    """
    body = [line for line in rest if line.strip(" \t")]
    head = first.strip(" \t")
    spelled = [first, *rest]
    start = next((i for i, line in enumerate(spelled) if line.strip(" \t")), len(spelled))
    inside = list(spelled[start:])
    while inside and not inside[-1].strip(" \t"):
        inside.pop()
    if head.startswith("[") or (not head and body and body[0].strip(" \t").startswith("[")):
        if any(not line.strip(" \t") for line in inside):
            return [UNPARSED_PATHS]
    if not head and body and re.match(r"^[ \t]*-[ \t]", body[0]):
        items = _block_list(body)
    elif head.startswith("[") or (
        not head and body and body[0].startswith(" ") and body[0].strip(" \t").startswith("[")
    ):
        items = _flow_list(" ".join(line.strip(" \t") for line in _strip_flow_comments([head, *body])).strip(" \t"))
    elif head:
        one = None if body else _scalar(head)
        items = [one] if one else None
    else:
        one = _scalar(body[0]) if len(body) == 1 and body[0][0] == " " else None
        items = [one] if one else None
    return items or [UNPARSED_PATHS]


#: A block scalar header (``|`` or ``>``, optional chomping mark, optional comment); an indentation digit is not read.
_BLOCK_HEADER = re.compile(r"^[|>][+-]?(?:[ \t]+#.*)?[ \t]*$")

#: A double-quoted scalar on one line, with only the escapes YAML defines, and an optional comment after it.
_DOUBLE_QUOTED = re.compile(
    r'^"(?:[^"\\]|\\(?:[0abt\tnvfre "/\\N_LP]|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U00(?:0[0-9a-fA-F]|10)[0-9a-fA-F]{4}))*"'
    r"(?:[ \t]+#.*)?[ \t]*$"
)

#: A single-quoted scalar on one line and an optional comment after it.
_SINGLE_QUOTED = re.compile(r"^'(?:[^']|'')*'(?:[ \t]+#.*)?[ \t]*$")

#: An empty flow list or mapping, with an optional comment after it.
_EMPTY_FLOW = re.compile(r"^(?:\[[ \t]*\]|\{[ \t]*\})(?:[ \t]+#.*)?[ \t]*$")

#: A block list item line: a dash alone or followed by a space, at any indent.
_LIST_ITEM = re.compile(r"^([ ]*)-(?:[ ]+(.*))?$")

#: A ``key: value`` line of a nested mapping (value optional), at any indent.
_NESTED_PAIR = re.compile(r"^([ ]*)[A-Za-z_][\w-]*[ \t]*:(?:[ \t]+(.*))?$")


#: Plain scalars YAML types that every loader builds without error: null, booleans, short decimal numbers, infinity.
#: Dates, merge and value tags, long digit runs and other number spellings are not in it (a bad date or a number too
#: long to convert raises while the document loads).
_SAFE_TYPED = re.compile(
    r"^(?:~|null|true|false|yes|no|on|off|y|n|[-+]?\.(?:inf|nan)|[-+]?(?:0|[1-9]\d{0,17})"
    r"|[-+]?\d{1,17}\.\d{0,17}(?:e[-+]?\d{1,3})?)$",
    re.IGNORECASE,
)


def _plain_value_ok(text: str) -> bool:
    """Report whether text is a plain scalar YAML reads in block context: no ``: `` or trailing colon, no indicator
    start."""
    plain = re.split(r"[ \t]#", text, maxsplit=1)[0].rstrip(" \t")
    return (
        bool(plain)
        and plain[0] not in _UNREAD_START
        and plain[0] not in "#'\""
        and not re.search(r":(?:[ \t]|$)", plain)
        and not re.match(r"[-?:](?:[ \t]|$)", plain)
        and (not _TYPED_PLAIN.match(plain) or bool(_SAFE_TYPED.match(plain)))
    )


def _inline_ok(text: str) -> bool:
    """Report whether a value written on one line is one YAML reads: empty, a closed quoted scalar, a flow list this
    reader follows, an empty flow collection, or a plain scalar."""
    if not text or text.startswith("#"):
        return True
    if text[0] == '"':
        return bool(_DOUBLE_QUOTED.match(text))
    if text[0] == "'":
        return bool(_SINGLE_QUOTED.match(text))
    if text[0] in "[{":
        if _EMPTY_FLOW.match(text):
            return True
        return text[0] == "[" and _flow_list(_strip_flow_comments([text])[0].strip(" \t")) is not None
    return _plain_value_ok(text)


def _plain_lines_ok(lines: list[str]) -> bool:
    """Report whether stripped lines read as one multi-line plain scalar: each is plain, and only the last has a
    comment.

    A comment ends a plain scalar, so text after it is no continuation.
    """
    return all(_plain_value_ok(line) for line in lines) and not any(re.search(r"[ \t]#", line) for line in lines[:-1])


def _nested_ok(content: list[str], inner_comment: bool) -> bool:
    """Report whether the lines under an empty ``key:`` read as one block list, one flat mapping or one plain scalar."""
    if not content:
        return True
    indents = {len(line) - len(line.lstrip(" ")) for line in content}
    if len(indents) != 1:
        return False
    items = [_LIST_ITEM.match(line) for line in content]
    if all(items):
        return all(_inline_ok((m.group(2) or "").strip(" \t")) for m in items if m)
    pairs = [_NESTED_PAIR.match(line) for line in content]
    if all(pairs):
        return all(_inline_ok((m.group(2) or "").strip(" \t")) for m in pairs if m)
    return (
        not inner_comment
        and not any(_LIST_ITEM.match(line) or _NESTED_PAIR.match(line) for line in content)
        and _plain_lines_ok([line.strip(" \t") for line in content])
    )


def _block_scalar_ok(more: list[str]) -> bool:
    """Report whether the lines under a ``|`` or ``>`` header form its text: all indented, none less than the first."""
    lines = [line for line in more if line.strip(" \t")]
    if any(line[0] != " " for line in lines):
        return False
    if not lines:
        return True
    first = len(lines[0]) - len(lines[0].lstrip(" "))
    before = more[: more.index(lines[0])]
    return all(len(line) <= first for line in before) and all(
        len(line) - len(line.lstrip(" ")) >= first for line in lines
    )


def _entry_readable(first: str, more: list[str]) -> bool:
    """Report whether one top-level ``key: value`` entry other than ``paths:`` is YAML this reader can prove valid."""
    value = first.partition(":")[2].strip(" \t")
    content = [line for line in more if line.strip(" \t") and not line.lstrip(" \t").startswith("#")]
    last = max((i for i, line in enumerate(more) if line in content), default=-1)
    inner_comment = any(line.lstrip(" \t").startswith("#") for line in more[:last])
    if not value or value.startswith("#"):
        return _nested_ok(content, inner_comment)
    if value[0] in "|>":
        return bool(_BLOCK_HEADER.match(value)) and _block_scalar_ok(more)
    if not _inline_ok(value):
        return False
    if value[0] in "[{'\"":
        return not content
    return (
        not inner_comment
        and all(line[0] == " " for line in content)
        and _plain_lines_ok([value, *(line.strip(" \t") for line in content)])
    )


def _other_entries_readable(lines: list[str]) -> bool:
    """Report whether every top-level entry in ``lines`` (the frontmatter outside ``paths:``) is provably valid YAML.

    PyYAML refuses the whole block when any entry is malformed, so a scope written beside one is not a scope. The
    check is deliberately narrower than YAML: a spelling it cannot prove (an anchor, a tag, a quoted line break, a
    flow mapping, a number in a flow list) makes the block unreadable, which only keeps the rule from merging.
    """
    entries: list[tuple[str, list[str]]] = []
    for line in lines:
        if not line.strip(" \t"):
            if entries:
                entries[-1][1].append(line)
        elif line[0] in " \t" or re.match(r"-(?:[ \t]|$)", line):
            if not entries:
                return False
            entries[-1][1].append(line)
        elif _PLAIN_KEY.match(line):
            entries.append((line, []))
        else:
            return False
    return all(_entry_readable(first, more) for first, more in entries)


def _plain_frontmatter(body: list[str]) -> bool:
    """Report whether every top-level line of a frontmatter block is a bare ``key:`` line or a list continuation.

    Valid YAML can spell a key as ``? key``, as a quoted or escaped string, or as part of a flow mapping, and a plain
    scan cannot tell which key that is. A block of any other shape is not read at all.
    """
    content = [line for line in body if line.strip(" \t") and not line.lstrip(" \t").startswith("#")]
    if not content:
        return True
    if content[0][0] in " \t":
        return False
    return all(line[0] in " \t" or _PLAIN_KEY.match(line) or line.startswith(("-", "[", "]")) for line in content)


def parse_paths(text: str) -> list[str]:
    """Return the ``paths:`` globs from a YAML frontmatter block, or an empty list when it has no ``paths:`` key.

    A block list, a flow list (inline, or continued on the following lines) and one plain or quoted scalar are read; a
    quoted ``"paths"`` key counts as the key. Everything else (a block or folded scalar, an anchor, an alias, a tag, a
    duplicate key, an unterminated list) is returned as ``UNPARSED_PATHS`` and never as an empty list, which would
    classify the rule as always loaded.
    """
    lines = _lines(text)
    if not lines or lines[0].strip(" \t") != "---":
        return []
    body: list[str] = []
    for line in lines[1:]:
        if line.strip(" \t") == "---":
            break
        body.append(line)
    if _YAML_UNREAD_CHAR.search("\n".join(body)) or not _plain_frontmatter(body):
        return [UNPARSED_PATHS]
    hits = [(index, match.group(1)) for index, line in enumerate(body) if (match := _PATHS_KEY.match(line))]
    # A key the column-0 reader did not account for (indented, or inside a flow mapping) is still a scope: refuse it.
    elsewhere = len(_ANY_PATHS_KEY.findall("\n".join(body))) != len(hits)
    if not hits and not elsewhere:
        return []
    if len(hits) != 1 or elsewhere:
        return [UNPARSED_PATHS]
    index, first = hits[0]
    rest: list[str] = []
    for follow in body[index + 1 :]:
        if follow.strip(" \t") and follow[0] not in " \t" and not follow.lstrip(" \t").startswith(("-", "[", "]", "#")):
            break
        rest.append(follow)
    if not _other_entries_readable([*body[:index], *body[index + 1 + len(rest) :]]):
        return [UNPARSED_PATHS]
    return _paths_value(first, rest)


def _lines(text: str) -> list[str]:
    """Split text into lines the way Markdown does: at ``\\n``, ``\\r\\n`` and ``\\r`` only.

    ``str.splitlines`` also breaks at U+2028, U+0085, form feed and others, which would move a fence or heading line
    boundary the reader and the renderer disagree on.
    """
    parts = re.split(r"\r\n|\r|\n", text)
    return parts[:-1] if parts and parts[-1] == "" else parts


def _closes_fence(line: str, fence: str) -> bool:
    """Report whether ``line`` closes the open fence ``fence`` (same character, at least as long, nothing after)."""
    match = _FENCE.match(line)
    return (
        bool(match)
        and match.group(1)[0] == fence[0]
        and len(match.group(1)) >= len(fence)
        and not line.strip(" \t").strip(fence[0])
    )


def _frontmatter_end(lines: list[str]) -> int:
    """Return the number of lines the YAML frontmatter occupies, or 0 when the file has none.

    An opening ``---`` that is never closed counts as frontmatter to the end of the file, so nothing in it is split.
    """
    if not lines or lines[0].strip(" \t") != "---":
        return 0
    return next((idx + 1 for idx, line in enumerate(lines[1:], 1) if line.strip(" \t") == "---"), len(lines))


def _starts_block(rest: str, item: bool) -> bool:
    """Report whether ``rest`` opens a block, so it cannot be a lazy continuation line of a paragraph in a closed
    container.

    The limits on what may interrupt a paragraph (a list item needs content, an ordered one starts at 1) hold only when
    the paragraph's own container is still open; for a lazy line the containers close first, so any item starts a list.
    When a list item is among the containers that close, an indented line counts by what follows its indentation: one
    that would be a heading, fence or HTML block inside the item is no paragraph text, so it closes the item (and is
    code at the outer level); an indented list marker is still paragraph text. A blockquote takes any indented line as
    text.
    """
    deep = len(rest) - len(rest.lstrip(" ")) >= 4
    rest = rest.lstrip(" ")
    if not rest or (deep and not item):
        return False
    if any(pattern.match(rest) for pattern in (_QUOTE, _ATX_LINE, _FENCE, _THEMATIC, _HTML_TO_BLANK)) or any(
        begins.match(rest) for begins, _ in _HTML_UNTIL
    ):
        return True
    return not deep and _list_start(rest, interrupts=False) is not None


def _list_start(rest: str, interrupts: bool) -> int | None:
    """Return the columns a list-item marker and its spaces take at the start of ``rest``, or None when it is no item.

    An item that interrupts a paragraph must have content, and an ordered one must start at 1. Five or more spaces after
    the marker belong to the content (an indented code block), so the item's content then starts one space after it.
    """
    match = _LIST_MARKER.match(rest)
    if not match:
        return None
    indent, marker, spaces, content = match.groups()
    if not content.strip(" \t"):
        return None if interrupts else len(indent) + len(marker) + 1
    if not spaces or (interrupts and marker[:-1].isdigit() and int(marker[:-1]) != 1):
        return None
    return len(indent) + len(marker) + (len(spaces) if len(spaces) < 5 else 1)


def _continues(stack: list[tuple[str, int, bool]], text: str) -> tuple[int, int]:
    """Return how many open containers the line continues and the column where its remaining text starts.

    A blockquote continues on a ``>`` marker; a list item on a blank line or on a line indented to the item's content,
    except an item that began with an empty line, which a second blank line closes.
    """
    pos = 0
    for matched, (kind, width, fresh) in enumerate(stack):
        rest = text[pos:]
        if kind == "q":
            marker = _QUOTE.match(rest)
            if not marker:
                return matched, pos
            pos += marker.end() + (1 if text[pos + marker.end() : pos + marker.end() + 1] == " " else 0)
        elif not rest.strip(" \t"):
            if fresh:
                return matched, pos
            pos = len(text)
        elif len(rest) - len(rest.lstrip(" ")) >= width:
            pos += width
        else:
            return matched, pos
    return len(stack), pos


def _item_closes(stack: list[tuple[str, int, bool]], matched: int) -> bool:
    """Report whether a list item is among the containers a line does not continue."""
    return any(kind == "l" for kind, _, _ in stack[matched:])


def _definition_span(rows: list[str]) -> int:
    """Return how many of the leading ``rows`` form one link reference definition, or 0 when they start none.

    The label, destination and title may each sit on a line of their own (one line ending between two parts), a title
    may run over several lines, and a title that is not closed cleanly at the end of its line is no title: the
    definition then ends with its destination, unless the title began on that same line, which makes the whole thing
    paragraph text.
    """
    text = "\n".join(rows)
    head = _DEF_HEAD.match(text)
    if not head or not re.sub(r"[\s\ufeff]+", "", head.group(1)) or len(head.group(1)) > 999:
        return 0
    dest = head.group(2)
    if not dest.startswith("<"):
        depth = 0
        for char in re.sub(r"\\.", "", dest):
            depth += (char == "(") - (char == ")")
            if depth < 0:
                return 0
        if depth:
            return 0
    tail = _DEF_TITLE.match(text, head.end()) or _DEF_EOL.match(text, head.end())
    return text[: tail.end()].count("\n") + 1 if tail else 0


def _interrupts(rest: str) -> bool:
    """Report whether a line starts a block that ends the text of a definition above it (any list item does)."""
    return rest.strip(" \t") != "" and (
        any(pattern.match(rest) for pattern in (_QUOTE, _ATX_LINE, _FENCE, _THEMATIC, _HTML_TO_BLANK))
        or any(begins.match(rest) for begins, _ in _HTML_UNTIL)
        or _list_start(rest, interrupts=False) is not None
    )


def _definition_rows(lines: list[str], stack: list[tuple[str, int, bool]], idx: int, first: str) -> list[str]:
    """Return the text rows from line ``idx`` on that a definition starting with ``first`` can run over.

    Only a line that is not blank and starts no block of its own can be part of it. A line that does not continue every
    open container still counts, as a lazy continuation of the paragraph the definition sits in.
    """
    rows = [first]
    for later in lines[idx + 1 :]:
        text = later.expandtabs(4)
        matched, pos = _continues(stack, text)
        rest = text[pos:]
        if not rest.strip(" \t"):
            break
        if matched < len(stack):
            if _starts_block(rest, item=_item_closes(stack, matched)):
                break
        elif len(rest) - len(rest.lstrip(" ")) < 4 and _interrupts(rest):
            break
        rows.append(rest.strip(" \t"))
    return rows


def _heading_title(text: str) -> str:
    """Return a heading's title without HTML comments and surrounding spaces."""
    return " ".join(_INLINE_COMMENT.sub("", text).split())


def _scan_blocks(
    lines: list[str],
    begin: int = 0,
    paragraphs: list[list[tuple[int, str]]] | None = None,
    stripped: dict[int, str] | None = None,
) -> list[tuple[int, int, str, int]]:
    """Return ``(line index, level, title, depth)`` of every heading from line ``begin``, as CommonMark reads them.

    One block reader for every question about headings (sections, citations, survival after an edit). It follows the
    CommonMark block rules a heading depends on: blockquotes and list items nest and close by their own continuation
    rules (a paragraph line may continue lazily), a code fence or HTML block ends with its container, ATX headings and
    setext headings (over the paragraph above the underline) are read inside any container, and an indented line is
    code. ``depth`` is the number of containers around the heading, 0 for a top-level one.

    When ``paragraphs`` is given, every paragraph the reader meets is appended to it as ``(line index, text)`` rows,
    the text of each row without the markers of its containers. When ``stripped`` is given it receives every line
    without the markers of the containers that line continues (the text inside a quoted code fence, say).
    """
    found: list[tuple[int, int, str, int]] = []
    stack: list[tuple[str, int, bool]] = []
    para: list[tuple[int, str]] = []
    fence = ""
    html_end: re.Pattern[str] | None = None
    to_blank = False
    consumed = 0
    for idx in range(begin, len(lines)):
        if paragraphs is not None and para and (not paragraphs or paragraphs[-1] is not para):
            paragraphs.append(para)
        text = lines[idx].expandtabs(4)
        matched, pos = _continues(stack, text)
        rest = text[pos:]
        if stripped is not None:
            stripped[idx] = rest
        in_leaf = bool(fence) or html_end is not None or to_blank
        if rest.strip(" \t"):
            stack[:matched] = [(kind, width, False) for kind, width, _ in stack[:matched]]
        if consumed:
            consumed -= 1
            continue
        if matched < len(stack):
            if (
                para
                and not in_leaf
                and not _starts_block(rest, item=_item_closes(stack, matched))
                and rest.strip(" \t")
            ):
                para.append((idx, rest.strip(" \t")))
                continue
            del stack[matched:]
            para, fence, html_end, to_blank = [], "", None, False
        if fence:
            fence = "" if _closes_fence(rest, fence) else fence
            continue
        if html_end is not None:
            html_end = None if html_end.search(rest) else html_end
            continue
        if to_blank:
            to_blank = bool(rest.strip(" \t"))
            continue
        while True:
            indent = len(rest) - len(rest.lstrip(" "))
            if not rest.strip(" \t"):
                para = []
            elif indent >= 4:
                if para:
                    para.append((idx, rest.strip(" \t")))
            elif para and (under := _SETEXT.match(rest)):
                found.append(
                    (para[0][0], 1 if under.group(1)[0] == "=" else 2, " ".join(t for _, t in para), len(stack))
                )
                para = []
            elif quote := _QUOTE.match(rest):
                para = []
                stack.append(("q", 0, False))
                rest = rest[quote.end() :]
                rest = rest[1:] if rest.startswith(" ") else rest
                continue
            elif atx := _ATX_LINE.match(rest):
                para = []
                found.append(
                    (idx, len(atx.group(1)), _ATX_CLOSE.sub("", (atx.group(2) or "").strip(" \t")), len(stack))
                )
            elif opened := _FENCE.match(rest):
                para, fence = [], opened.group(1)
            elif start := next((end for begins, end in _HTML_UNTIL if begins.match(rest)), None):
                para, html_end = [], (None if start.search(rest, 1) else start)
            elif _HTML_TO_BLANK.match(rest) or (not para and _HTML_TAG_ALONE.match(rest)):
                para, to_blank = [], True
            elif _THEMATIC.match(rest):
                para = []
            elif (taken := _list_start(rest, interrupts=bool(para))) is not None:
                para = []
                rest = rest[taken:]
                stack.append(("l", taken, not rest.strip(" \t")))
                continue
            elif para or not (span := _definition_span(_definition_rows(lines, stack, idx, rest.strip(" \t")))):
                para.append((idx, rest.strip(" \t")))
            else:
                consumed = span - 1
            break
    if paragraphs is not None and para and (not paragraphs or paragraphs[-1] is not para):
        paragraphs.append(para)
    return sorted((idx, level, title, depth) for idx, level, raw, depth in found if (title := _heading_title(raw)))


def _scan_headings(lines: list[str], begin: int = 0) -> list[tuple[int, int, str]]:
    """Return ``(line index, level, title)`` of every top-level heading from line ``begin`` (not inside any
    container)."""
    return [(idx, level, title) for idx, level, title, depth in _scan_blocks(lines, begin) if depth == 0]


def split_sections(text: str) -> list[tuple[str, int, int, str]]:
    """Split Markdown into ``(title, start_line, end_line, text)`` sections, ignoring headings inside code fences.

    A fence closes only on a run of the same character at least as long as the one that opened it, so a four-backtick
    block can hold three-backtick examples. YAML frontmatter is its own ``(frontmatter)`` section, and the lines between
    it and the first heading form a ``(preamble)`` section.
    """
    lines = _lines(text)
    front_end = _frontmatter_end(lines)
    starts: list[tuple[int, str]] = (
        [(0, "(frontmatter)"), (front_end, "(preamble)")] if front_end else [(0, "(preamble)")]
    )
    starts += [(idx, title) for idx, level, title in _scan_headings(lines, front_end) if level <= _SECTION_LEVEL]
    sections: list[tuple[str, int, int, str]] = []
    for pos, (begin, title) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
        body = "\n".join(lines[begin:end])
        if body.strip():
            sections.append((title, begin + 1, end, body))
    return sections


def build_pairs(plugin_dir: Path) -> dict[str, str]:
    """Map each plugin-relative stub and ``_full`` path to its counterpart, for pairs whose two files exist."""
    pairs: dict[str, str] = {}
    for full in sorted((plugin_dir / "rules" / "_full").glob("*.md")):
        stub = PAIR_STUBS.get(full.stem, f"rules/{full.stem}.md")
        if (plugin_dir / stub).is_file():
            pairs[stub] = f"rules/_full/{full.name}"
            pairs[f"rules/_full/{full.name}"] = stub
    return pairs


def build_alternatives(plugin_dir: Path) -> dict[str, str]:
    """Map a rule to its install-alternative id when the plugin ships both ``X.md`` and ``X-delta.md``.

    The setup skill delivers one of the two (the delta when foundry is present, the full rule otherwise), so they are
    never loaded together and a section of one must not absorb a section of the other.
    """
    alternatives: dict[str, str] = {}
    for delta in sorted((plugin_dir / "rules").glob(f"*{ALT_SUFFIX}.md")):
        full = delta.with_name(delta.name.removesuffix(f"{ALT_SUFFIX}.md") + ".md")
        if full.is_file():
            alternatives[f"rules/{delta.name}"] = f"alt:{delta.stem}"
            alternatives[f"rules/{full.name}"] = f"alt:{full.stem}"
    return alternatives


def classify(
    rel: str, text: str, pairs: dict[str, str] | None = None, alternatives: dict[str, str] | None = None
) -> tuple[str, Load]:
    """Return ``(kind, load)`` for a plugin-relative instruction file path."""
    pairs = pairs or {}
    parts = Path(rel).parts
    pair = pairs.get(rel, "")
    side = "stub" if pair else ""
    top = parts[0] if parts else ""
    if parts == ("CLAUDE.src.md",):
        return "claude-md", Load("always", "session start", side=side, pair=pair, owner="CLAUDE.src.md")
    if top == "rules" and "_full" in parts:
        return "rule-full", Load("lazy", "stub pointer read on demand", side="full", pair=pair, owner=pair or rel)
    if top == "rules":
        paths = parse_paths(text)
        if paths and not set(paths) <= ALWAYS_GLOBS:
            # An unreadable paths: value gets a group of its own, so such rules are never merged with each other.
            owner = rel if UNPARSED_PATHS in paths else (alternatives or {}).get(rel, "")
            return "rule", Load("scoped", "file access matching paths:", paths=paths, side=side, pair=pair, owner=owner)
        return "rule", Load("always", "session start", side=side, pair=pair, owner=(alternatives or {}).get(rel, ""))
    if top == "agents":
        return "agent", Load("lazy", "agent spawned", owner="agents")
    if top == "skills" and len(parts) > 1:
        shared = parts[1] == "_shared"
        owner = "/".join(parts) if shared else "/".join(parts[:2])
        if parts[-1] == "SKILL.md":
            return "skill", Load("lazy", "skill invoked", owner=owner)
        return "skill-fragment", Load("lazy", "loaded by the owning skill step", owner=owner)
    return "other", Load("lazy", "unknown")


def _claude_plugins(root: Path) -> list[Path]:
    """Return the plugin directories Claude loads from: every ``cc_*`` plugin and any other with a Claude manifest."""
    plugins = root / "plugins"
    if not plugins.is_dir():
        return []
    return sorted(
        p
        for p in plugins.iterdir()
        if p.is_dir() and (p.name.startswith("cc_") or (p / ".claude-plugin" / "plugin.json").is_file())
    )


def _manifest(plugin: Path) -> dict[str, object]:
    """Return a plugin's ``plugin.json`` as a dict, or an empty one when it is missing or unreadable."""
    try:
        data = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _registered(plugin: Path, value: object) -> list[Path]:
    """Return the paths a manifest component field names: one ``./`` path or a list of them, inside the plugin."""
    paths: list[Path] = []
    for entry in value if isinstance(value, list) else [value]:
        if not isinstance(entry, str) or not (entry == "." or entry.startswith("./")):
            continue
        named = posixpath.normpath(entry.replace("\\", "/"))
        if not named.startswith(("/", "..")) and ":" not in named:
            paths.append(plugin if named == "." else plugin / named)
    return paths


def _skill_roots(plugin: Path) -> list[Path]:
    """Return the directories a plugin loads skills from: the default ``skills/`` and each one its manifest registers.

    The manifest ``skills`` value is a path or a list of paths, each starting with ``./`` (or ``.`` for the plugin root)
    and added to the default directory. An entry that leaves the plugin directory is ignored.
    """
    roots = [plugin / "skills", *_registered(plugin, _manifest(plugin).get("skills"))]
    return [root for root in dict.fromkeys(roots) if root.is_dir()]


def _skill_files(plugin: Path) -> list[Path]:
    """Return the skill instruction files of a plugin: every Markdown under a registered directory except its docs.

    A registration of the plugin root itself names its ``SKILL.md`` (one skill) and the ``SKILL.md`` files one level
    down, both when both exist. A plugin with no skill directory and a ``SKILL.md`` at its root is a single skill.
    """
    roots = _skill_roots(plugin)
    found: list[Path] = []
    for root in roots:
        if root != plugin:
            found += sorted(p for p in root.rglob("*.md") if p.name not in SKILL_DOC_NAMES)
            continue
        if (plugin / "SKILL.md").is_file():
            found += _root_skill_files(plugin)
        for child in _child_skills(plugin):
            found += sorted(p for p in child.rglob("*.md") if p.name not in SKILL_DOC_NAMES)
    if not roots and (plugin / "SKILL.md").is_file():
        found += _root_skill_files(plugin)
    return found


def _child_skills(plugin: Path) -> list[Path]:
    """Return the folders directly under the plugin root that hold a ``SKILL.md``, whatever the folder is called.

    A folder named like another component (``docs``, ``scripts``) is still a skill once it holds ``SKILL.md`` and the
    plugin registers its root: the file is what makes a folder a skill, not its name.
    """
    return [
        folder
        for folder in sorted(plugin.iterdir())
        if folder.is_dir() and not folder.name.startswith(".") and (folder / "SKILL.md").is_file()
    ]


def _root_skill_files(plugin: Path) -> list[Path]:
    """Return a skill kept at the plugin root: its ``SKILL.md`` and the Markdown beside it that other components do not
    own."""
    found = [plugin / "SKILL.md"]
    found += sorted(p for p in plugin.glob("*.md") if p.name not in SKILL_DOC_NAMES | _NOT_SKILL_FILES | {"SKILL.md"})
    for folder in sorted(plugin.iterdir()):
        if folder.is_dir() and folder.name not in _NOT_SKILL_DIRS and not folder.name.startswith("."):
            found += sorted(p for p in folder.rglob("*.md") if p.name not in SKILL_DOC_NAMES)
    return found


def _command_files(plugin: Path) -> list[Path]:
    """Return the command files of a plugin: what its manifest ``commands`` names, which replaces ``commands/``.

    The manifest value is a path or a list of paths (a ``.md`` file or a directory of them) or an object map whose
    entries name a ``source`` file; an entry with inline ``content`` has no file. Without the key, every Markdown under
    ``commands/`` is a command. A command is loaded when it is invoked, as a skill is.
    """
    declared = _manifest(plugin).get("commands")
    if declared is None:
        named = [plugin / "commands"]
    elif isinstance(declared, dict):
        named = _registered(plugin, [v.get("source") for v in declared.values() if isinstance(v, dict)])
    else:
        named = _registered(plugin, declared)
    found: list[Path] = []
    for path in named:
        if path.is_dir():
            found += sorted(path.rglob("*.md"))
        elif path.suffix == ".md" and path.is_file():
            found.append(path)
    return list(dict.fromkeys(found))


def _agent_files(plugin: Path) -> list[Path]:
    """Return the agent files of a plugin: the ``.md`` files its manifest lists, which replace ``agents/``, else every
    Markdown under ``agents/`` (a subfolder is part of the agent's name)."""
    declared = _manifest(plugin).get("agents")
    if isinstance(declared, (str, list)):
        return [p for p in _registered(plugin, declared) if p.suffix == ".md" and p.is_file()]
    return sorted((plugin / "agents").rglob("*.md"))


def _loaded_rel(plugin: Path, plugin_rel: str) -> str:
    """Return a file's plugin-relative path as if it sat in the default ``skills/`` or ``agents/`` directory.

    A directory of skills is read as ``skills/``; a folder holding ``SKILL.md`` directly is one skill, named after the
    folder, so its fragments share its group; a registered agent file is read as ``agents/<name>``; a command file is
    read as a fragment of the plugin's commands under ``skills/_commands/``.
    """
    if not plugin_rel.startswith("agents/") and (plugin / plugin_rel) in _agent_files(plugin):
        return "agents/" + Path(plugin_rel).name
    if (plugin / plugin_rel) in _command_files(plugin):
        return "skills/_commands/" + plugin_rel
    roots = _skill_roots(plugin)
    for root in sorted(roots, key=lambda r: -len(r.parts)):
        if root == plugin:
            if any(plugin_rel.startswith(child.name + "/") for child in _child_skills(plugin)):
                return "skills/" + plugin_rel
            if (plugin / "SKILL.md").is_file():
                return f"skills/{plugin.name}/{plugin_rel}"
            continue
        base = root.relative_to(plugin).as_posix()
        if not plugin_rel.startswith(base + "/") or (base == "skills" and not (root / "SKILL.md").is_file()):
            continue
        inside = plugin_rel[len(base) + 1 :]
        return f"skills/{root.name}/{inside}" if (root / "SKILL.md").is_file() else "skills/" + inside
    if not roots and (plugin / "SKILL.md").is_file():
        return f"skills/{plugin.name}/{plugin_rel}"
    return plugin_rel


def discover(root: Path, kinds: tuple[str, ...]) -> list[Path]:
    """List the instruction files of the Claude plugin source tree for the requested kinds.

    Rules and ``CLAUDE.src.md`` come from the ``cc_*`` plugins; skills and agents come from every Claude plugin: the
    skill directories it loads (``skills/`` and the ones its manifest adds) and its agent files.
    """
    found: list[Path] = []
    for plugin in _claude_plugins(root):
        own = plugin.name.startswith("cc_")
        if own and "rules" in kinds:
            found += sorted((plugin / "rules").glob("*.md")) + sorted((plugin / "rules" / "_full").glob("*.md"))
            found += [p for p in [plugin / "CLAUDE.src.md"] if p.is_file()]
        if "skills" in kinds:
            found += _skill_files(plugin) + _command_files(plugin)
        if "agents" in kinds:
            found += _agent_files(plugin)
    return [p for p in dict.fromkeys(found) if p.is_file()]


def shared_index() -> dict[str, str]:
    """Map every MANIFEST canonical and copy path to a short role description."""
    index: dict[str, str] = {}
    for entry in MANIFEST:
        canonical = str(entry["canonical"])
        index[canonical] = f"canonical of {len(list(entry['copies']))} copies"  # type: ignore[call-overload]
        for copy in entry["copies"]:  # type: ignore[attr-defined]
            index[str(copy)] = f"copy of {canonical}"
    return index


def shared_digest() -> str:
    """Return one sha256 over the shared-file membership that blocks merging.

    Which files are byte-identical copies is decided by the propagation manifest, not by any inventoried file's text, so
    a change to it alters merge eligibility while every file digest stays equal.
    """
    return hashlib.sha256(json.dumps(shared_index(), sort_keys=True).encode("utf-8")).hexdigest()


#: A backslash escape or a character reference, decoded in one pass so an escaped ``&`` stays a literal ``&amp;``.
_ESCAPE_OR_ENTITY = re.compile(
    r"\\([!-/:-@\[-`{-~])|(&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});)"
)

#: A CommonMark raw HTML tag (attribute values in quotes may hold ``>``): an open or closing tag, a processing
#: instruction, a declaration, a CDATA section or a comment.
_HTML_TAG = re.compile(
    r"<[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*(?:\s*=\s*(?:[^\s\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*\s*/?>"
    r"|</[A-Za-z][A-Za-z0-9-]*\s*>|<\?.*?\?>|<![A-Za-z][^>]*>|<!\[CDATA\[.*?\]\]>|<!--.*?-->",
    re.DOTALL,
)

#: A cited file name followed by a section sign: where the text cited for one file ends and the next file's begins.
_FILE_SIGN = re.compile(r"[\w./\\-]+\.md[`'\"\])]*\s*\(?\s*§")

#: Up to three words just before a cited file name, in case the name itself holds spaces (``my rules.md``), even when
#: a line break falls between them.
_LEAD_WORDS = re.compile(r"((?:[\w./\\-]+\s+){1,3})$")

#: Most characters of the text after a section sign that are kept to test headings against.
_MAX_TAIL_CHARS = 2000

#: A quoted title right after a section sign: its ``;`` and ``|`` belong to the title, not to the citation list.
_QUOTED_TITLE = re.compile(
    r"§\s*(?:\"[^\"\n]*\"|\u201c[^\u201d\n]*\u201d|`[^`\n]*`|'[^'\n]*'|\u2018[^\u2019\n]*\u2019)"
)

#: A CommonMark autolink (``<scheme:rest>`` or ``<name@host>``): the text between the angle brackets is shown.
_AUTOLINK = re.compile(
    r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*"
    r"|[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)>"
)


def _link_target_end(text: str, pos: int) -> int:
    """Return the index after the ``)`` that closes a link target starting at ``pos``, or -1 when it is no target.

    A target is a destination (``<...>``, or a run without spaces or control characters and with balanced parentheses),
    then optionally white space and a title in quotes or parentheses. A backslash keeps the next character from ending
    or opening anything.
    """
    size = len(text)

    def skip_space(at: int) -> int:
        while at < size and text[at] in " \t\n":
            at += 1
        return at

    def find_close(at: int, mark: str) -> int:
        while at < size and text[at] != mark:
            at += 2 if text[at] == "\\" else 1
        return at if at < size else -1

    pos = skip_space(pos)
    if pos < size and text[pos] == "<":
        close = find_close(pos + 1, ">")
        if close < 0 or "\n" in text[pos:close]:
            return -1
        pos = close + 1
    else:
        depth = 0
        while pos < size and text[pos] > " " and text[pos] != "\x7f":
            if text[pos] == "\\":
                pos += 2
                continue
            depth += (text[pos] == "(") - (text[pos] == ")")
            if depth < 0:
                break
            pos += 1
        if depth > 0:
            return -1
    after = skip_space(pos)
    if after > pos and after < size and text[after] in "\"'(":
        end = find_close(after + 1, {"(": ")"}.get(text[after], text[after]))
        if end < 0:
            return -1
        after = skip_space(end + 1)
    return after + 1 if after < size and text[after] == ")" else -1


def _drop_link_targets(text: str) -> str:
    """Return ``text`` without the ``(target "title")`` that follows each unescaped ``]`` of an inline link or image."""
    kept: list[str] = []
    done = 0
    scan = 0
    while (hit := text.find("](", scan)) >= 0:
        slashes = len(text[:hit]) - len(text[:hit].rstrip("\\"))
        end = -1 if slashes % 2 else _link_target_end(text, hit + 2)
        if end < 0:
            scan = hit + 2
            continue
        kept.append(text[done : hit + 1])
        done = scan = end
    return "".join(kept) + text[done:]


def _plain_title(title: str) -> str:
    """Return a title without its inline Markup: backslash escapes, HTML tags and entities, link and image targets, code
    ticks and emphasis marks, in Unicode compatibility form.

    A heading is written ``##### **CI**``, ``## <code>CI</code>`` or ``## C\\*I`` and cited as ``§CI`` or ``§C*I``; both
    sides must reduce to the same words, so an escaped mark and a bare one are dropped alike.
    """
    text = _ESCAPE_OR_ENTITY.sub(lambda m: m.group(1) or html.unescape(m.group(2)), _drop_link_targets(title))
    text = _AUTOLINK.sub(r"\1", unicodedata.normalize("NFKC", text))
    text = _HTML_TAG.sub("", text)
    text = re.sub(r"\]\[[^\[\]]*\]", "]", text)
    text = re.sub(r"[\[\]]|!(?=\[)", "", text)
    return re.sub(r"[`*~]+|(?<!\w)_+|_+(?!\w)", "", text)


def _normalize_title(title: str) -> str:
    """Return a heading or citation title reduced to the words that identify it."""
    cut = re.split(r"\s+[—(]|[(),.:]", _plain_title(title), maxsplit=1)[0]
    return " ".join(cut.split()).casefold()


@dataclass(frozen=True)
class Mention:
    """One ``<file> §<Heading>`` mention: the path as written, the heading cited, the file that cites it, and the text
    that follows its section sign up to the end of the line (or the next cited file)."""

    path: str
    heading: str
    citing: str
    tail: str = ""
    lead: str = ""


def _markdown_files(root: Path) -> list[Path]:
    """Return every Markdown file under ``plugins/``: the files whose text can cite a section."""
    return sorted((root / "plugins").rglob("*.md"))


def citation_digest(root: Path) -> str:
    """Return one sha256 over the path and content digest of every Markdown file that can cite a section.

    Citations come from all plugin Markdown, not only the inventoried files, so a change to any of them can change which
    sections are protected; per-file digests of the inventoried files alone would miss it.
    """
    lines = [
        f"{p.relative_to(root).as_posix()}:{hashlib.sha256(p.read_bytes()).hexdigest()}" for p in _markdown_files(root)
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _citation_units(text: str) -> list[str]:
    """Split Markdown into the units a citation can run within, as the block reader sees them.

    A unit is one paragraph with the markers of its containers (quote, list item) removed, so a citation wrapped over
    the lines of a quoted or listed paragraph reads like one on a single line; each heading's title is a unit of its
    own, so one heading never runs on into the next; every other run of non-blank lines (code, HTML, definitions) is a
    unit read without container markers. A blank line, a heading or a new block ends a unit.
    """
    lines = _lines(text)
    paragraphs: list[list[tuple[int, str]]] = []
    stripped: dict[int, str] = {}
    found = _scan_blocks(lines, paragraphs=paragraphs, stripped=stripped)
    owned = {idx for para in paragraphs for idx, _ in para}
    heads = {idx for idx, _, _, _ in found}
    units = ["\n".join(row for _, row in para) for para in paragraphs]
    units += [title for idx, _, title, _ in found if idx not in owned]
    run: list[str] = []
    for idx, line in enumerate(lines):
        if idx not in owned and idx not in heads and line.strip(" \t"):
            run.append(stripped.get(idx, line))
        elif run:
            units.append("\n".join(run))
            run = []
    if run:
        units.append("\n".join(run))
    return units


def _section_text(unit: str, match: re.Match[str], limit: int) -> str:
    """Return the ``§...`` text of a citation, carried over the line breaks of a wrapped list of sections.

    A list of sections that wraps (``f.md §A,`` then ``§B`` on the next line of the unit) is one citation, as it is on a
    single line. It ends at a ``;`` or ``|``, or where the line holds the next cited file; ``limit`` is where the unit
    or that file begins.
    """
    parts = ["§" + match.group(2) + match.group(3)]
    pos = match.end()
    while pos < limit and unit[pos] == "\n":
        end = unit.find("\n", pos + 1)
        line = unit[pos + 1 : min(len(unit) if end < 0 else end, limit)]
        piece = re.split(r"[;|]", line, maxsplit=1)[0]
        parts.append(piece)
        if piece != line:
            break
        pos += 1 + len(line)
    return "\n".join(parts)


def _mentions(root: Path, tail_chars: int = _MAX_TAIL_CHARS) -> list[Mention]:
    """Return every ``<file> §<Heading>`` mention found in any Markdown under ``plugins/``.

    A backtick or quote between the file name and ``§`` is accepted, and ``§A, §B`` yields two mentions. The cited text
    is kept for ``tail_chars`` characters.
    """
    found: list[Mention] = []
    for path in _markdown_files(root):
        raw = path.read_text(encoding="utf-8", errors="replace").replace("\x00", " ").replace("\x01", " ")
        rel = path.relative_to(root).as_posix()
        for unit in _citation_units(raw):
            text = _QUOTED_TITLE.sub(lambda m: m.group(0).replace(";", "\x00").replace("|", "\x01"), unit)
            for match in _CITATION.finditer(text):
                next_file = _FILE_SIGN.search(text, match.start(2))
                limit = next_file.start() if next_file else len(text)
                tail = text[match.start(2) : limit][:tail_chars].replace("\x00", ";").replace("\x01", "|")
                lead_words = _LEAD_WORDS.search(text, max(0, match.start(1) - 80), match.start(1))
                lead = lead_words.group(1) if lead_words else ""
                for opener, piece in _SECTION_ITEM.findall(_section_text(text, match, limit)):
                    piece = piece.replace("\x00", ";").replace("\x01", "|")
                    if "`" in opener:
                        piece = piece.split("`")[0]
                    elif piece.count("`") % 2:
                        piece = piece[: piece.rindex("`")]
                    piece = _CLOSING_MARKS.split(piece)[0] if opener else piece
                    if heading := _normalize_title(piece.strip(" \t\"'*_\u201c\u201d\u2018\u2019")):
                        found.append(Mention(match.group(1), heading, rel, tail, lead))
    return found


def _scope_root(file: str) -> str:
    """Return the skill directory of a skill file, else the plugin directory: the tree that shares its bare names."""
    parts = Path(file).parts
    if len(parts) > 4 and parts[2] in ("skills", "claude-skills"):
        return "/".join(parts[:4])
    return "/".join(parts[:2])


def _narrow(matches: list[str], citing: str) -> set[str]:
    """Keep the matches inside the citing file's own tree (its skill, then its plugin), else one match found anywhere.

    Several same-named files elsewhere are ambiguous, so none of them is credited: a mention is never guessed onto a
    file it may not have named.
    """
    for tree in (_scope_root(citing), "/".join(Path(citing).parts[:2])):
        near = {m for m in matches if m.startswith(tree + "/")}
        if near:
            return near
    return set(matches) if len(matches) == 1 else set()


def resolve_mention(mention: Mention, by_name: dict[str, list[str]], plugins: set[str]) -> set[str]:
    """Return the inventoried files a mention names, found by looking them up, never by inferring from the name.

    Order: a ``./`` or ``../`` path taken relative to the citing file, and nothing else when it names no inventoried
    file (never a looser reading); the path as a tail of an existing file. Only a bare name, or one under the
    installed rules directory, may then be read as the delivered form ``<plugin>-<name>.md``, which names that
    plugin's ``<name>.md`` if the plugin has one: a path that was written out and names no file is a miss, never a
    guess. Backslash separators are read as ``/``. A path found in several trees is narrowed by ``_narrow``.
    """
    path = mention.path.replace("\\", "/")
    if path.startswith(("./", "../")):
        target = posixpath.normpath(posixpath.join(posixpath.dirname(mention.citing), path))
        return {target} if target in by_name.get(posixpath.basename(target), []) else set()
    name = posixpath.basename(path)
    direct = [f for f in by_name.get(name, []) if f == path or f.endswith("/" + path)]
    if found := _narrow(direct, mention.citing):
        return found
    if posixpath.dirname(path) not in ("", ".claude/rules") and not posixpath.dirname(path).endswith("/.claude/rules"):
        return set()
    delivered: set[str] = set()
    for plugin in plugins:
        if name.startswith(f"{plugin}-"):
            stripped = name.removeprefix(f"{plugin}-")
            delivered |= {f for f in by_name.get(stripped, []) if Path(f).parts[1] == f"cc_{plugin}"}
    return delivered


def _tail_key(tail: str) -> str:
    """Return the text after a section sign as one comparable string: opening marks dropped, markup and case folded."""
    return _full_title(tail.lstrip(" \t`'\"*_\u201c\u2018"))


def _starts_with_title(tail_key: str, title_key: str) -> bool:
    """Report whether the cited text begins with a heading's whole title and goes on at a word boundary.

    Testing the heading against the text, not a title cut out of the text, means a title holding ``;``, ``|`` or a
    section sign is found however it is written.
    """
    size = len(title_key)
    return (
        bool(title_key) and tail_key.startswith(title_key) and (len(tail_key) == size or not tail_key[size].isalnum())
    )


def _tail_chars(root: Path, files: list[str]) -> int:
    """Return how many characters of cited text to keep: enough for the longest heading of the inventoried files.

    Compatibility folding can make a normalised title longer than the heading it came from, hence the margin.
    """
    longest = 0
    for file in files:
        text = (root / file).read_text(encoding="utf-8", errors="replace")
        longest = max([longest, *(len(title) for _, _, title, _ in _scan_blocks(_lines(text)))])
    return max(_MAX_TAIL_CHARS, 20 * longest + 64)


def citation_index(root: Path, files: list[str]) -> dict[str, list[tuple[str, ...]]]:
    """Map each inventoried file to the ``(heading, citing file, cited text)`` mentions that resolve to it.

    ``files`` are the repo-relative paths the inventory covers; a mention that resolves to none of them cites nothing
    the inventory can merge or delete, so it is dropped.
    """
    by_name: dict[str, list[str]] = {}
    for file in files:
        by_name.setdefault(posixpath.basename(file), []).append(file)
    plugins = {p.name.removeprefix("cc_") for p in (root / "plugins").glob("cc_*")}
    index: dict[str, list[tuple[str, ...]]] = {}
    for mention in _mentions(root, _tail_chars(root, files)):
        words = [*mention.lead.split(), mention.path]
        for start in range(len(words)):
            named = replace(mention, path=" ".join(words[start:]))
            if hits := resolve_mention(named, by_name, plugins):
                for hit in hits:
                    index.setdefault(hit, []).append((mention.heading, mention.citing, _tail_key(mention.tail)))
                break
    return index


def _same_heading(wanted: str, cited: str) -> bool:
    """Report whether a cited heading names a section: equal titles, a citation that runs on into prose after the whole
    title, or one a prefix of the other when both are long.

    The prefix reading covers a citation that drops a heading's tail, and the whole-word reading one that runs on into
    prose (``§CI and`` for ``CI``); below four characters a bare prefix would match too much, so a short title such as
    ``CI`` is cited by an equal title or by one that continues after a space, never the other way round.
    """
    if not wanted or not cited:
        return False
    if wanted == cited or cited.startswith(wanted + " "):
        return True
    return len(wanted) >= 4 and len(cited) >= 4 and (wanted.startswith(cited) or cited.startswith(wanted))


def _headings(text: str, first_line: int = 1, deep_only: bool = False) -> list[tuple[str, int]]:
    """Return ``(title, line)`` for the headings of ``text`` outside code fences; ``line`` counts from ``first_line``.

    A heading inside a blockquote or list item counts at any level: it is never a section boundary, so the deep-only
    filter that keeps a section's level-5/6 headings does not apply to it.
    """
    found = _scan_blocks(_lines(text))
    return [
        (title, first_line + idx)
        for idx, level, title, depth in found
        if depth or not deep_only or level > _SECTION_LEVEL
    ]


def _guarded(file: str, title: str, start: int, body: str, index: dict[str, list[tuple[str, ...]]]) -> list[dict]:
    """Return the headings of a section, its own and its level-5/6 ones, that other files cite, with the citing files.

    These are the headings that must still exist after the section is changed, so each carries its own line.
    """
    found: list[dict] = []
    for heading, line in [(title, start), *_headings(body, start, deep_only=True)]:
        if refs := inbound_refs(file, [heading], index):
            found.append({"title": heading, "line": line, "refs": refs})
    return found


def inbound_refs(item_file: str, titles: list[str], index: dict[str, list[tuple[str, ...]]]) -> list[str]:
    """Return the other files that cite a section, by its title or by a deeper heading inside it, as ``<file>
    §<Heading>``.

    A mention counts when its cited title names a heading (``_same_heading``) or when the text after its section sign
    begins with a heading's whole title (``_starts_with_title``); an entry without that text is read by the first rule.
    """
    wanted = [_normalize_title(title) for title in titles]
    whole = [_full_title(title) for title in titles]
    return sorted(
        {
            citing
            for heading, citing, *rest in index.get(item_file, [])
            if citing != item_file
            and (
                any(_same_heading(title, heading) for title in wanted)
                or (rest and any(_starts_with_title(str(rest[0]), title) for title in whole))
            )
        }
    )


def sibling_targets(payload: str) -> list[str]:
    """Return the repo-relative paths named in a ``policy-sibling`` marker payload."""
    return [m.group(0).rstrip(".") for m in _SIBLING_PATH.finditer(payload)]


def section_for_line(sections: list[tuple[str, int, int, str]], line: int) -> int:
    """Return the index of the section containing a 1-based line, or -1."""
    for idx, (_, start, end, _) in enumerate(sections):
        if start <= line <= end:
            return idx
    return -1


def group_id(plugin: str, load: Load) -> str:
    """Return the merge-scope identity: sections may only merge inside one group.

    A group is the load hierarchy, the owning plugin (plugins stay independent), and a detail: the exact path globs for
    scoped rules; for always-loaded rules the install variant (``CLAUDE.src.md`` and a rule that has a foundry-present
    delta each stand alone); for lazy sections the whole skill (every file it can load), the plugin's agents, or one
    stub pair.
    """
    # JSON of the sorted set keeps ['a,b'] apart from ['a', 'b'] and makes glob order irrelevant.
    scoped_by_glob = load.hierarchy == "scoped" and not load.owner
    detail = json.dumps(sorted(set(load.paths)), separators=(",", ":")) if scoped_by_glob else load.owner
    return f"{load.hierarchy}:{plugin}:{detail}"


def merge_allowed(group: str) -> bool:
    """Report whether sections of ``group`` may be merged; lazy groups never co-load unconditionally."""
    return group.startswith(("always:", "scoped:"))


def _block_dependencies(items: list[Item], sections: list[tuple[str, int, int, str]], text: str, root: Path) -> None:
    """Record on ``items`` every dependency that makes a merge unsafe: shared copy, frontmatter, policy siblings."""
    for item in items:
        if item.shared:
            item.blocked.append(f"shared-file: {item.shared}")
    if sections and sections[0][0] == "(frontmatter)":
        items[0].blocked.append("frontmatter: routing or scope metadata (description, trigger, paths)")
    for marker in _SIBLING.finditer(text):
        line = text.count("\n", 0, marker.start()) + 1
        targets = sibling_targets(marker.group(1))
        missing = [t for t in targets if not (root / t).exists()]
        owner = section_for_line(sections, line)
        file_wide = owner < 0 or sections[owner][0] in ("(preamble)", "(frontmatter)")
        for item in items if file_wide else [items[owner]]:
            item.siblings.extend(targets)
            item.blocked.append("policy-sibling: " + ", ".join(targets))
            if missing or not targets:
                item.blocked.append("policy-sibling-unresolved: " + ", ".join(missing or ["no path"]))


def build_items(
    root: Path,
    path: Path,
    shared: dict[str, str],
    cites: dict[str, list[tuple[str, ...]]],
    pairs: dict[str, str] | None = None,
    alternatives: dict[str, str] | None = None,
) -> list[Item]:
    """Build the inventory items for one instruction file, resolving its merge-blocking dependencies."""
    rel = path.relative_to(root).as_posix()
    plugin = path.relative_to(root / "plugins").parts[0]
    plugin_rel = Path(*path.relative_to(root / "plugins").parts[1:]).as_posix()
    plugin_rel = _loaded_rel(root / "plugins" / plugin, plugin_rel)
    text = path.read_text(encoding="utf-8", errors="replace")
    kind, load = classify(plugin_rel, text, pairs, alternatives)
    group = group_id(plugin, load)
    sections = split_sections(text)
    guards = [_guarded(rel, title, start, body, cites) for title, start, _, body in sections]
    items = [
        Item(
            id=f"{rel}#{n}",
            file=rel,
            section=title,
            start_line=start,
            end_line=end,
            tokens=estimate_tokens(body),
            kind=kind,
            load=load,
            plugin=plugin,
            group=group,
            inbound=sorted({ref for guard in guards[n] for ref in guard["refs"]}),
            shared=shared.get(rel, ""),
            guarded=guards[n],
        )
        for n, (title, start, end, body) in enumerate(sections)
    ]
    _block_dependencies(items, sections, text, root)
    return items


def _excluded_docs(root: Path, kinds: tuple[str, ...]) -> list[str]:
    """Return the skill documentation files the inventory skips on purpose (they are never loaded as instructions)."""
    if "skills" not in kinds:
        return []
    return sorted(
        p.relative_to(root).as_posix()
        for plugin in _claude_plugins(root)
        for skills in _skill_roots(plugin)
        if skills != plugin
        for p in skills.rglob("*.md")
        if p.name in SKILL_DOC_NAMES
    )


def build_inventory(root: Path, kinds: tuple[str, ...]) -> dict[str, object]:
    """Build the full inventory document for the requested kinds."""
    base = root.resolve()
    found = discover(root, kinds)
    files = [path for path in found if path.resolve().is_relative_to(base)]
    outside = {path.relative_to(root).as_posix() for path in found} - {
        path.relative_to(root).as_posix() for path in files
    }
    shared = shared_index()
    cites = citation_index(root, [path.relative_to(root).as_posix() for path in files])
    contexts: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
    items: list[Item] = []
    for path in files:
        plugin_dir = root / "plugins" / path.relative_to(root / "plugins").parts[0]
        pairs, alternatives = contexts.setdefault(
            plugin_dir.name, (build_pairs(plugin_dir), build_alternatives(plugin_dir))
        )
        items += build_items(root, path, shared, cites, pairs, alternatives)
    summary: dict[str, dict[str, int]] = {}
    for item in items:
        # An install alternative replaces its sibling, so its tokens are not part of the always-loaded set.
        key = "always-alternative" if item.load.owner.startswith("alt:") else item.load.hierarchy
        bucket = summary.setdefault(key, {"items": 0, "tokens": 0, "blocked": 0})
        bucket["items"] += 1
        bucket["tokens"] += item.tokens
        bucket["blocked"] += 1 if item.blocked else 0
    return {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "kinds": list(kinds),
        "digests": {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
        "citation_digest": citation_digest(root),
        "shared_digest": shared_digest(),
        "excluded": sorted({*_excluded_docs(root, kinds), *outside}),
        "summary": summary,
        "groups": rank_groups(items),
        "items": [asdict(item) for item in items],
    }


def rank_groups(items: list[Item]) -> list[dict[str, object]]:
    """Rank merge-scope groups by the tokens in them that no dependency blocks, largest first."""
    groups: dict[str, dict[str, object]] = {}
    for item in items:
        bucket = groups.setdefault(item.group, {"items": 0, "tokens": 0, "unblocked_tokens": 0, "files": set()})
        bucket["items"] += 1  # type: ignore[operator]
        bucket["tokens"] += item.tokens  # type: ignore[operator]
        bucket["unblocked_tokens"] += 0 if item.blocked else item.tokens  # type: ignore[operator]
        bucket["files"].add(item.file)  # type: ignore[attr-defined]
    ranked = [
        {"group": name, **{k: (len(v) if k == "files" else v) for k, v in bucket.items()}, "merge": merge_allowed(name)}
        for name, bucket in groups.items()
    ]
    return sorted(ranked, key=lambda g: (-int(g["unblocked_tokens"]), str(g["group"])))  # type: ignore[call-overload]


def group_kind(group: str) -> str:
    """Return ``rules``, ``skills`` or ``agents`` for a group id."""
    detail = group.split(":", 2)[2] if group.count(":") >= 2 else ""
    if detail.startswith("skills/"):
        return "skills"
    return "agents" if detail == "agents" else "rules"


def _token_groups(token: str, groups: list[str], agent_plugins: dict[str, set[str]]) -> set[str]:
    """Return the groups one scope token names: a kind, ``plugins``, a plugin, an agent or a skill."""
    plugin = token if token.startswith("cc_") else f"cc_{token}"
    hits: set[str] = set()
    for group in groups:
        group_plugin, detail = group.split(":", 2)[1:]
        if (
            (token in ALL_KINDS and group_kind(group) == token)
            or token in ALL_PLUGINS_TOKEN
            or group_plugin in (plugin, token)
            or (detail == "agents" and group_plugin in agent_plugins.get(token, set()))
            or detail == f"skills/{token}"
        ):
            hits.add(group)
    return hits


def _balance(groups: list[dict[str, object]], count: int) -> list[list[str]]:
    """Split groups into at most ``count`` batches of near-equal unblocked tokens, largest first."""
    bins: list[tuple[int, list[str]]] = [(0, []) for _ in range(max(1, min(count, len(groups))))]
    for entry in sorted(groups, key=lambda g: -int(g["unblocked_tokens"])):  # type: ignore[call-overload]
        smallest = min(range(len(bins)), key=lambda i: bins[i][0])
        weight, members = bins[smallest]
        bins[smallest] = (weight + int(entry["unblocked_tokens"]), [*members, str(entry["group"])])  # type: ignore[call-overload]
    return [members for _, members in bins if members]


def select_groups(
    inventory: dict[str, object], scope: str, min_tokens: int, max_groups: int, batches: int
) -> dict[str, object]:
    """Choose the groups to examine from the ranked inventory and say what was left out and why.

    The scope is a union, as in the audit skill's own grammar: each token names a kind (``rules``, ``skills``,
    ``agents``), ``plugins`` (everything), a plugin, an agent file or a skill, and a group is in scope when any token
    names it. A token that names nothing is returned under ``unmatched_tokens``; no tokens means everything.
    """
    tokens = [t for t in re.split(r"[\s,]+", scope.strip()) if t]
    ranked: list[dict[str, object]] = inventory["groups"]  # type: ignore[assignment]
    names = [str(g["group"]) for g in ranked]
    agent_plugins: dict[str, set[str]] = {}
    for item in inventory["items"]:  # type: ignore[attr-defined]
        if item["kind"] == "agent":
            agent_plugins.setdefault(Path(item["file"]).stem, set()).add(item["plugin"])
    matched = {t: _token_groups(t, names, agent_plugins) for t in tokens}
    unmatched = [t for t, hits in matched.items() if not hits]
    wanted = set().union(*matched.values()) if tokens else set(names)
    in_scope = [g for g in ranked if g["group"] in wanted]
    in_scope.sort(key=lambda g: not g["merge"])  # stable: groups that can be merged and cost every session come first
    chosen: list[dict[str, object]] = []
    left: list[dict[str, object]] = []
    for entry in in_scope:
        reason = ""
        if int(entry["files"]) < 2:  # type: ignore[call-overload]
            reason = "single-file group (Check 29 and efficiency E8 own it)"
        elif int(entry["unblocked_tokens"]) < min_tokens:  # type: ignore[call-overload]
            reason = "below token floor"
        elif len(chosen) >= max_groups:
            reason = "group cap"
        if reason:
            left.append({"group": entry["group"], "unblocked_tokens": entry["unblocked_tokens"], "reason": reason})
        else:
            chosen.append(entry)
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": tokens,
        "unmatched_tokens": unmatched,
        "selected": [{k: e[k] for k in ("group", "items", "files", "unblocked_tokens", "merge")} for e in chosen],
        "batches": _balance(chosen, batches),
        "not_examined": left,
    }


#: Item fields that locate a section in its file.
_SPAN_KEYS = ("id", "file", "section", "start_line", "end_line")


def _blocked_ranges(members: list[dict[str, object]], index: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    """Return the blocked sections of the files the cluster's members live in: no edit may touch them."""
    files = {m["file"] for m in members}
    return [{k: item[k] for k in _SPAN_KEYS} for item in index.values() if item["file"] in files and item["blocked"]]


def _claim_reasons(cid: object, distinct: list[str], index: dict[str, dict[str, object]], seen: set[str]) -> list[str]:
    """Return the reasons a cluster id or its member list is unusable regardless of load scope."""
    reasons = []
    if not isinstance(cid, str) or not _CLUSTER_ID.fullmatch(cid):
        reasons.append(f"unsafe cluster id {cid!r}: use letters, digits, '-' and '_' only")
    elif cid.lower() in seen:
        reasons.append(f"duplicate cluster id {cid} (ids are compared case-insensitively: they become file names)")
    if len(distinct) < 2:
        reasons.append("fewer than two distinct members")
    files = {str(index[i]["file"]) for i in distinct if i in index}
    if len(distinct) >= 2 and len(files) == 1:
        reasons.append("every member is in one file (Check 29 and efficiency E8 own single-file repetition)")
    return reasons


def _file_guards(files: set[str], index: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    """Return every cited heading of every section in ``files``, members or not.

    An edit confined to a member can still change how the rest of its file reads (an opening code fence swallows what
    follows it), so survival is checked for all the file's cited headings, not only the members' own.
    """
    return [
        {"id": item["id"], "file": item["file"], **guard}
        for item in index.values()
        if item["file"] in files
        for guard in item["guarded"]
    ]


def validate_cluster(
    cluster: dict[str, object],
    index: dict[str, dict[str, object]],
    seen: set[str] | None = None,
    floor: int = MIN_CLUSTER_TOKENS,
) -> dict[str, object]:
    """Vet one proposed cluster against the inventory and return its findings.

    Member overlap between clusters is settled afterwards by ``_assign_judging``, which knows which clusters will be
    judged; here a cluster is judged on its own members only.
    """
    seen = set() if seen is None else seen
    raw_id = cluster.get("id")
    cid = raw_id if isinstance(raw_id, str) else ""
    raw = cluster.get("items", [])
    ids = [str(i) for i in raw] if isinstance(raw, list) else []
    distinct = list(dict.fromkeys(ids))
    members = [index[i] for i in distinct if i in index]
    unknown = [i for i in distinct if i not in index]
    blocked = {str(m["id"]): m["blocked"] for m in members if m["blocked"]}
    scopes = sorted({str(m["group"]) for m in members})
    sides = {(str(m["load"]["side"]), str(m["load"]["pair"])) for m in members if m["load"]["side"]}  # type: ignore[index]
    stubs = {str(m["file"]) for m in members if m["load"]["side"] == "stub"}  # type: ignore[index]
    fulls = {str(m["load"]["pair"]) for m in members if m["load"]["side"] == "full"}  # type: ignore[index]
    flattened = sorted(f for f in stubs if any(f.endswith(pair) for pair in fulls))
    reasons = _claim_reasons(raw_id, distinct, index, seen)
    if len(ids) != len(distinct):
        reasons.append("duplicate members in the cluster")
    if unknown:
        reasons.append("unknown members: " + ", ".join(unknown))
    reasons += [f"blocked member {i}: {'; '.join(map(str, why))}" for i, why in blocked.items()]
    if len(scopes) > 1:
        reasons.append("mixed load scopes: " + ", ".join(scopes))
    if flattened:
        reasons.append("flattens a stub/_full pair: " + ", ".join(flattened))
    seen.add(cid.lower())
    ok = not reasons
    tokens = sum(int(m["tokens"]) for m in members)
    mergeable = len(scopes) == 1 and merge_allowed(scopes[0])
    most_saved = tokens - max((int(m["tokens"]) for m in members), default=0) if mergeable else 0
    return {
        "id": cid,
        "ok": ok,
        "judge": ok and _worth_judging(tokens, most_saved, mergeable, floor),
        "reasons": reasons,
        "tokens_before": tokens,
        "max_saving": most_saved,
        "always_loaded_tokens_before": sum(int(m["tokens"]) for m in members if m["load"]["hierarchy"] == "always"),  # type: ignore[index]
        "load_class": str(members[0]["load"]["hierarchy"]) if members and len(scopes) == 1 else "",  # type: ignore[index]
        "merge_allowed": mergeable,
        "items": ids,
        "members": distinct,
        "locations": [{k: m[k] for k in _SPAN_KEYS} for m in members],
        "blocked_ranges": _blocked_ranges(members, index),
        "inbound": {str(m["id"]): m["inbound"] for m in members if m["inbound"]},
        "guarded": [{"id": m["id"], "file": m["file"], **g} for m in members for g in m["guarded"]],
        "file_guarded": _file_guards({str(m["file"]) for m in members}, index),
        "unknown_members": unknown,
        "blocked_members": blocked,
        "mixed_scopes": scopes if len(scopes) > 1 else [],
        "pair_flattened": flattened,
        "sides": sorted(f"{s} (pair {p})" for s, p in sides),
    }


def _worth_judging(tokens: int, most_saved: int, mergeable: bool, floor: int) -> bool:
    """Report whether a valid cluster can repay a judge.

    It must hold ``floor`` tokens. A cluster that can be merged must also be able to save at least half the floor: a
    merge keeps at least its largest member, so the most it saves is the rest of the cluster. Sharpen-only (lazy)
    clusters save nothing by design and are judged for clarity, so only the size floor applies to them.
    """
    return tokens >= floor and (not mergeable or most_saved >= floor // 2)


def _skip_reason(cluster: dict[str, object], floor: int) -> str:
    """Return why a valid cluster is not judged: under the size floor, under the saving floor, or past the cap."""
    if int(cluster["tokens_before"]) < floor:  # type: ignore[call-overload]
        return "below cluster floor"
    if cluster["merge_allowed"] and int(cluster["max_saving"]) < floor // 2:  # type: ignore[call-overload]
        return "below saving floor"
    return "judge cap"


def _assign_judging(vetted: list[dict[str, object]], max_judged: int, floor: int) -> None:
    """Choose which valid clusters are judged, and reject judged clusters that share a member.

    Best means mergeable first, then largest: a mergeable cluster is the only kind that can save tokens in every
    session, and a larger one has more to save. Only a cluster that will actually be judged claims its members, so a
    cluster that is under the floor or past the cap never blocks a better one that overlaps it. Every other valid
    cluster records why it is not judged.
    """
    ranked = sorted(
        (c for c in vetted if c["judge"]),
        key=lambda c: (not c["merge_allowed"], -int(c["tokens_before"])),  # type: ignore[call-overload]
    )
    claimed: dict[str, str] = {}
    judged = 0
    for cluster in ranked:
        rivals = sorted({claimed[m] for m in cluster["members"] if m in claimed})  # type: ignore[attr-defined]
        if rivals:
            cluster["ok"] = cluster["judge"] = False
            cluster["reasons"].append("members already claimed by " + ", ".join(rivals))  # type: ignore[attr-defined]
        elif judged >= max_judged:
            cluster["judge"] = False
        else:
            claimed.update({m: str(cluster["id"]) for m in cluster["members"]})  # type: ignore[attr-defined]
            judged += 1
    for cluster in vetted:
        if cluster["ok"] and not cluster["judge"]:
            cluster["skip_reason"] = _skip_reason(cluster, floor)


def validate(
    inventory: dict[str, object],
    clusters: list[dict[str, object]],
    floor: int = MIN_CLUSTER_TOKENS,
    max_judged: int = MAX_JUDGED,
) -> dict[str, object]:
    """Vet every cluster against the inventory; a member belongs to the first valid cluster that claims it.

    ``judge`` is true for at most ``max_judged`` clusters above the token ``floor``, mergeable ones first.
    """
    index = {str(i["id"]): i for i in inventory["items"]}  # type: ignore[attr-defined]
    seen: set[str] = set()
    vetted = [validate_cluster(c, index, seen, floor) for c in clusters]
    _assign_judging(vetted, max_judged, floor)
    return {"schema_version": SCHEMA_VERSION, "floor": floor, "max_judged": max_judged, "clusters": vetted}


#: How severe each review verdict is; when one cluster has several verdicts the most severe counts.
_VERDICT_RANK = {"holds": 0, "weakens": 1, "loses": 2}


#: An edit's ``lines`` value: one line number or ``<start>-<end>``.
_LINES = re.compile(r"\s*(\d{1,9})\s*(?:[-–]\s*(\d{1,9}))?\s*")


def _edit_span(edit: dict[str, object]) -> tuple[int, int] | None:
    """Return an edit's 1-based inclusive ``(start, end)`` line range, or None when it is missing, malformed or
    inverted."""
    match = _LINES.fullmatch(str(edit.get("lines", "")))
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2) or start)
    return (start, end) if start <= end else None


def _bounds_problem(edit: dict[str, object], file: str, vet: dict[str, object]) -> str:
    """Return why an edit to a member file lies outside the cluster's sections or inside a blocked one, else ''."""
    span = _edit_span(edit)
    if span is None:
        return f"edit in {file} has no parseable or an inverted 'lines' range"
    start, end = span
    for blocked in vet.get("blocked_ranges", []):  # type: ignore[attr-defined]
        if blocked["file"] == file and start <= blocked["end_line"] and end >= blocked["start_line"]:
            return f"edit {file}:{start}-{end} touches blocked section {blocked['id']}"
    inside = any(
        loc["file"] == file and loc["start_line"] <= start and end <= loc["end_line"]
        for loc in vet["locations"]  # type: ignore[attr-defined]
    )
    return "" if inside else f"edit {file}:{start}-{end} lies outside the cluster's sections"


def _full_title(title: str) -> str:
    """Return a heading title for an exact comparison: case and spacing folded, nothing else dropped."""
    return " ".join(_plain_title(title).split()).casefold()


def _heading_titles(text: str) -> Counter[str]:
    """Return how often each full title occurs among the real headings of ``text``, at any level.

    Counting occurrences, not just titles, keeps one of two equal headings from vouching for the other.
    """
    return Counter(_full_title(title) for title, _ in _headings(text))


def _keeps_heading(edit: dict[str, object], title: str) -> bool:
    """Report whether a rewrite edit's new text still carries a heading with this title (a fenced copy is not one)."""
    return edit.get("action") == "rewrite" and _full_title(title) in _heading_titles(str(edit.get("after", "")))


def _simulate(text: str, edits: list[dict[str, object]], add_after: bool) -> str | None:
    """Return the file text after applying ``edits``, or None when an edit's range does not fit the file.

    Replacing and deleting edits act on their lines. An ``add`` carries one line number and the judge's convention is
    not fixed, so the caller applies it both before and after that line (``add_after``).
    """
    lines = _lines(text)
    placed: list[tuple[int, int, dict[str, object]]] = []
    for edit in edits:
        span = _edit_span(edit)
        if span is None or span[0] < 1 or span[1] > len(lines):
            return None
        placed.append((span[0], span[1], edit))
    for start, end, edit in sorted(placed, key=lambda item: -item[0]):
        new = [] if edit.get("action") == "delete" else _lines(str(edit.get("after", "")))
        if edit.get("action") == "add":
            at = end if add_after else start - 1
            lines[at:at] = new
        else:
            lines[start - 1 : end] = new
    return "\n".join(lines)


def _overlaps(edits: list[dict[str, object]]) -> bool:
    """Report whether two edits to one file cover a common line."""
    spans = sorted(span for edit in edits if (span := _edit_span(edit)))
    return any(later[0] <= earlier[1] for earlier, later in pairwise(spans))


def _lost_headings(guards: list[dict], edits: list[dict[str, object]], text: str | None) -> list[dict]:
    """Return the guarded headings that no longer exist after ``edits`` are applied to a file.

    With the file text, the edits are applied and the result is re-read by the same heading reader the inventory used,
    so an edit that turns a heading into code (an opening fence before it), indents it out of being a heading, or
    deletes it is caught whatever its wording. Without the text, an edit covering the heading's line counts as removing
    it unless a rewrite carries the heading.
    """
    if text is not None:
        after = [_simulate(text, edits, add_after) for add_after in (True, False)]
        if all(result is not None for result in after):
            before = _heading_titles(text)
            kept = [_heading_titles(str(result)) for result in after]
            return [
                g
                for g in guards
                if any(c[_full_title(str(g["title"]))] < before[_full_title(str(g["title"]))] for c in kept)
            ]
    lost = []
    for guard in guards:
        for edit in edits:
            span = _edit_span(edit)
            if span and span[0] <= guard["line"] <= span[1] and not _keeps_heading(edit, str(guard["title"])):
                lost.append(guard)
                break
    return lost


def _cited_problems(
    edits: list[object], vet: dict[str, object], repoint: object, sources: dict[str, str] | None = None
) -> list[str]:
    """Return the edits that remove a cited heading, at any depth, without listing every citing file under repoint."""
    repointed = {str(r) for r in repoint if isinstance(r, str)} if isinstance(repoint, list) else set()
    by_file: dict[str, list[dict[str, object]]] = {}
    for edit in edits:
        if isinstance(edit, dict) and isinstance(edit.get("file"), str):
            by_file.setdefault(str(edit["file"]), []).append(edit)
    problems: list[str] = []
    for file, group in by_file.items():
        if _overlaps(group):
            problems.append(f"edits to {file} overlap")
        guards = [g for g in vet["file_guarded"] if g["file"] == file and set(g["refs"]) - repointed]  # type: ignore[attr-defined]
        for guard in _lost_headings(guards, group, (sources or {}).get(file)):
            unlisted = set(guard["refs"]) - repointed
            problems.append(
                f"edits remove the heading {guard['title']!r} of {guard['id']}, cited by "
                f"{', '.join(sorted(unlisted))} (list them under repoint)"
            )
    return problems


#: Characters of text that must leave some files and arrive in others before a skill or agent sharpen is flagged for it.
_MIN_TRANSFER_CHARS = 20


def _removed_chars(edit: dict[str, object], file: str, sources: dict[str, str] | None) -> int:
    """Return the characters (whitespace folded) an edit removes: the real lines of its range, else the judge's
    quote."""
    span = _edit_span(edit)
    text = (sources or {}).get(file)
    if span is not None and text is not None:
        return len(" ".join(" ".join(_lines(text)[span[0] - 1 : span[1]]).split()))
    return len(" ".join(str(edit.get("before", "")).split()))


def _edit_problems(
    edits: list[object], vet: dict[str, object], repoint: object, sources: dict[str, str] | None = None
) -> tuple[bool, list[str], bool]:
    """Inspect an edit list; return ``(needs_review, reasons the edits are unusable, crosses_files)``.

    Only an edit that is a dict, has ``action == "add"`` and an empty ``before`` skips independent review; any other
    shape, action or spelling is treated as removing text. An edit must name a file of the cluster and a ``lines`` range
    wholly inside one of the cluster's sections and clear of every blocked section, an edit that removes the heading of
    a cited section needs every citing file under ``repoint``, and the same text added to several places needs review.
    ``crosses_files`` is set, in a group where merging is not allowed, when at least ``_MIN_TRANSFER_CHARS`` of net text
    leave some files and arrive in others. Whether that is a move is a judgment, so it goes to the independent reviewer
    flagged, never to a string test.
    """
    members = {str(loc["file"]) for loc in vet["locations"]}  # type: ignore[attr-defined]
    problems: list[str] = []
    needs_review = False
    net: dict[str, int] = {}
    texts: list[str] = []
    for edit in edits:
        if not isinstance(edit, dict):
            needs_review = True
            continue
        target = edit.get("file")
        if not isinstance(target, str) or target not in members:
            problems.append(f"edit targets {target!r}, outside the cluster")
        elif problem := _bounds_problem(edit, target, vet):
            problems.append(problem)
        file = str(target)
        if not (edit.get("action") == "add" and not edit.get("before")):
            needs_review = True
            net[file] = net.get(file, 0) - _removed_chars(edit, file, sources)
        if edit.get("action") in ("add", "rewrite", "pointer"):
            net[file] = net.get(file, 0) + len(" ".join(str(edit.get("after", "")).split()))
            texts.append(str(edit.get("after", "")))
    if len({t for t in texts if t}) < len([t for t in texts if t]):
        needs_review = True
    gained = sum(v for v in net.values() if v > 0)
    lost = -sum(v for v in net.values() if v < 0)
    crosses = not vet["merge_allowed"] and min(gained, lost) >= _MIN_TRANSFER_CHARS
    problems += _cited_problems(edits, vet, repoint, sources)
    return needs_review, list(dict.fromkeys(problems)), crosses


def _obligation_problems(obligations: object, members: list[str], proposal: str) -> list[str]:
    """Return what is missing from an obligation map: fields, modal strength, proposal wording, member coverage."""
    if not isinstance(obligations, list) or not obligations:
        return ["no obligation map"]
    try:
        json.dumps(obligations, allow_nan=False)
    except (TypeError, ValueError):
        return ["obligation map is not plain JSON"]
    flat = " ".join(proposal.split())
    problems: list[str] = []
    covered: set[str] = set()
    for entry in obligations:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(k), str) and entry[k].strip() for k in OBLIGATION_FIELDS
        ):
            problems.append("an obligation lacks original, from or to")
            continue
        covered.add(entry["from"])
        if entry.get("modal_preserved") is not True:
            problems.append("an obligation does not preserve its modal strength")
        if " ".join(entry["to"].split()) not in flat:
            problems.append("an obligation's 'to' text is not in the proposal")
    missing = [m for m in members if m not in covered]
    if missing:
        problems.append("no obligation recorded for " + ", ".join(missing))
    return list(dict.fromkeys(problems))


def _merge_losses(
    vet: dict[str, object], survivor: str, text: str, repointed: set[str], sources: dict[str, str] | None
) -> set[str]:
    """Return the files citing a heading outside the cluster that merging would hide.

    The merge is applied as edits: the survivor's lines become the proposal and every other member's lines go. A
    proposal that opens a code fence or a comment swallows the sections after it, whose headings are not the cluster's
    own, and one that opens with a line of text over a setext underline folds the heading above it into a new title. The
    survivor's own cited headings are therefore read again in the whole file the edits produce.
    """
    members = {str(m) for m in vet["members"]}  # type: ignore[attr-defined]
    by_file: dict[str, list[dict[str, object]]] = {}
    for loc in vet["locations"]:  # type: ignore[attr-defined]
        keep = loc["id"] == survivor
        edit = {
            "file": loc["file"],
            "action": "rewrite" if keep else "delete",
            "before": "",
            "after": text if keep else "",
        }
        by_file.setdefault(str(loc["file"]), []).append(edit | {"lines": f"{loc['start_line']}-{loc['end_line']}"})
    lost: set[str] = set()
    for file, group in by_file.items():
        guards = [
            g
            for g in vet["file_guarded"]  # type: ignore[attr-defined]
            if g["file"] == file and (g["id"] not in members or g["id"] == survivor)
        ]
        guards = [g for g in guards if set(g["refs"]) - repointed]
        for guard in _lost_headings(guards, group, (sources or {}).get(file)):
            lost |= set(guard["refs"])
    return lost


def _read_text(path: Path) -> str | None:
    """Return a file's UTF-8 text, or None when it is missing or not text."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _merge_blockers(
    judged: dict[str, object], vet: dict[str, object], text: str | None, sources: dict[str, str] | None = None
) -> list[str]:
    """Return the reasons a merge-prune decision cannot stand as judged."""
    reasons = []
    if not vet["merge_allowed"]:
        reasons.append("merge not allowed in this load group (lazy files never co-load unconditionally)")
    raw = judged.get("survivor")
    survivor = raw if isinstance(raw, str) else ""
    if survivor not in vet["members"]:  # type: ignore[operator]
        reasons.append("survivor is not a member of the cluster")
    kept: Counter[str] = Counter()
    if text is None:
        reasons.append("proposal file missing or not UTF-8 text")
    else:
        if len(text.encode("utf-8")) // 3 >= int(vet["tokens_before"]):  # type: ignore[call-overload]
            reasons.append("proposal is not shorter than its members")
        reasons += _obligation_problems(judged.get("obligation_map"), vet["members"], text)  # type: ignore[arg-type]
        kept = _heading_titles(text)
    if judged.get("edits"):
        # The proposal is the whole change; a side edit would grow or alter text the savings and the review never count.
        reasons.append("a merge-prune's change is its proposal: it carries no edits (list citing files under repoint)")
    repoint = judged.get("repoint")
    repointed = {str(r) for r in repoint if isinstance(r, str)} if isinstance(repoint, list) else set()
    # A merged-away member loses every cited heading; the survivor loses a title only when its proposal carries fewer
    # headings of that title than the survivor had cited ones (one equal heading must not vouch for another).
    owed = Counter(_full_title(str(g["title"])) for g in vet["guarded"] if g["id"] == survivor)  # type: ignore[attr-defined]
    short = {title for title, count in owed.items() if kept[title] < count}
    cited = {
        ref
        for guard in vet["guarded"]  # type: ignore[attr-defined]
        if guard["id"] != survivor or _full_title(str(guard["title"])) in short
        for ref in guard["refs"]
    }
    if text is not None:
        cited |= _merge_losses(vet, survivor, text, repointed, sources)
    if cited - repointed:
        reasons.append(
            "deleted or renamed sections are cited by "
            + ", ".join(sorted(cited - repointed))
            + " (list them under repoint)"
        )
    return reasons


def _apply_review(decision: str, verdict: str, needs_review: bool, advice_only: bool) -> tuple[str, str]:
    """Return ``(decision, validation)`` after the independent review verdict, if any.

    A ``weakens`` or ``loses`` verdict counts even when review was not required, so a reviewer who looked anyway is
    never overridden. Advice with no concrete edit cannot be reviewed and stays ``unvalidated``.
    """
    if decision not in ("merge-prune", "sharpen"):
        return decision, "not-required"
    if verdict == "weakens":
        return "rejected", "weakens"
    if verdict == "loses":
        return "keep", "loses"
    if advice_only:
        return decision, "unvalidated"
    if not needs_review:
        return decision, "not-required"
    return decision, "holds" if verdict == "holds" else "unvalidated"


@dataclass
class Classified:
    """A judge decision after the deterministic rules: what it became and what review it needs."""

    decision: str
    downgrades: list[str] = field(default_factory=list)
    advice_only: bool = False
    edit_review: bool = False
    crosses_files: bool = False


def _classify(
    judged: dict[str, object], vet: dict[str, object], text: str | None, sources: dict[str, str] | None = None
) -> Classified:
    """Apply the deterministic rules to one judge decision."""
    decision = judged.get("decision")
    if decision not in DECISIONS:
        return Classified("keep", [f"unrecognised decision {decision!r}"])
    unreadable = sorted({str(loc["file"]) for loc in vet["locations"]} - set(sources)) if sources is not None else []  # type: ignore[attr-defined]
    if unreadable and decision in ("merge-prune", "sharpen"):
        return Classified("rejected", [f"cannot read {', '.join(unreadable)} to check the change"])
    if decision == "merge-prune":
        blockers = _merge_blockers(judged, vet, text, sources)
        return Classified("sharpen", blockers, advice_only=True) if blockers else Classified("merge-prune")
    if decision == "sharpen":
        edits = judged.get("edits")
        if not isinstance(edits, list) or not edits:
            return Classified("sharpen", ["sharpen lists no concrete edit"], advice_only=True)
        review, problems, crosses = _edit_problems(edits, vet, judged.get("repoint"), sources)
        if problems:
            return Classified("rejected", problems, edit_review=review, crosses_files=crosses)
        return Classified("sharpen", edit_review=review, crosses_files=crosses)
    return Classified(str(decision))


def _change_digest(
    kind: str,
    vet: dict[str, object],
    judged: dict[str, object],
    proposal: str | None,
    crosses: bool,
    sources: dict[str, str] | None = None,
) -> str:
    """Return a sha256 over exactly what an independent reviewer is asked to judge for one cluster.

    It covers every field of the reviewer's entry (locations, citing files, guarded headings, repoint, the flag that
    text must not move between files, and the proposal, obligations or edits), plus the load facts that decide what the
    reviewer is told is allowed. A verdict written for any other input is never applied to this one.
    """
    material: dict[str, object] = {
        "cluster": vet["id"],
        "kind": kind,
        "locations": vet["locations"],
        "inbound": vet["inbound"],
        "guarded": vet["guarded"],
        "merge_allowed": vet["merge_allowed"],
        "load_class": vet["load_class"],
        "crosses_files": crosses,
        "repoint": judged.get("repoint", []),
        # The text under review: a verdict for the same proposal over changed originals must not carry over.
        "originals": {f: hashlib.sha256(t.encode("utf-8")).hexdigest() for f, t in sorted((sources or {}).items())},
    }
    if kind == "merge-prune":
        material |= {
            "survivor": judged.get("survivor", ""),
            "obligations": judged.get("obligation_map", []),
            "proposal": proposal or "",
        }
    else:
        material["edits"] = judged.get("edits", [])
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def _read_sources(root: Path | None, vet: dict[str, object]) -> dict[str, str] | None:
    """Return the text of the files a cluster's sections live in, or None when no repository root is known."""
    if root is None:
        return None
    base = root.resolve()
    sources: dict[str, str] = {}
    for loc in vet["locations"]:  # type: ignore[attr-defined]
        path = (base / str(loc["file"])).resolve()
        if path.is_relative_to(base) and path.is_file():
            sources[str(loc["file"])] = path.read_text(encoding="utf-8", errors="replace")
    return sources


def settle_cluster(
    judged: dict[str, object],
    vet: dict[str, object],
    proposals: Path,
    reviews: dict[str, dict[str, object]] | None,
    sources: dict[str, str] | None = None,
    root: Path | None = None,
) -> dict[str, object]:
    """Apply the deterministic rules to one judge decision and return its settled record.

    ``reviews`` maps a change digest to the independent verdict written for that change; a verdict for any other digest
    is ignored and noted.
    """
    cid = str(vet["id"])
    proposal = proposals / f"{cid}.md"
    obligations = proposals / f"{cid}.obligations.json"
    text = _read_text(proposal)
    classified = _classify(judged, vet, text, sources)
    decision, downgrades, advice_only = classified.decision, classified.downgrades, classified.advice_only
    needs_review = decision == "merge-prune" or (decision == "sharpen" and classified.edit_review)
    kind_reviewed = decision in ("merge-prune", "sharpen")
    digest = _change_digest(decision, vet, judged, text, classified.crosses_files, sources) if kind_reviewed else ""
    review = (reviews or {}).get(digest)
    if reviews and review is None:
        downgrades = [*downgrades, "ignored a review written for a different change (its digest does not match)"]
    verdict = str(review.get("verdict", "")) if review else ""
    decision, validation = _apply_review(decision, verdict, needs_review, advice_only)
    merged = decision == "merge-prune"
    after = len((text or "").encode("utf-8")) // 3 if merged else 0
    if root is not None and not _allowed_output(obligations, root):
        raise ValueError(f"{obligations} is not a run artifact location; this mode writes only run artifacts")
    if merged:
        _write_artifact(obligations, json.dumps(judged["obligation_map"], indent=2, allow_nan=False) + "\n")
    else:
        obligations.unlink(missing_ok=True)
    return {
        "cluster": cid,
        "judged": judged.get("decision", ""),
        "decision": decision,
        "validation": validation,
        "needs_review": needs_review and not advice_only,
        "advice_only": advice_only,
        "downgrades": downgrades,
        "crosses_files": classified.crosses_files,
        "digest": digest,
        "reason": judged.get("reason", ""),
        "edits": judged.get("edits") if isinstance(judged.get("edits"), list) else [],
        "evidence": review.get("evidence") if review and isinstance(review.get("evidence"), str) else "",
        "load_class": vet["load_class"],
        "tokens_before": vet["tokens_before"],
        "tokens_after": after,
        "savings": int(vet["tokens_before"]) - after if merged else 0,  # type: ignore[call-overload]
        "survivor": judged.get("survivor", ""),
        "proposal": str(proposal) if merged else "",
        "obligations": str(obligations) if merged else "",
        "locations": vet["locations"],
        "inbound": vet["inbound"],
        "guarded": vet["guarded"],
    }


def _review_input(record: dict[str, object], judged: dict[str, object]) -> dict[str, object]:
    """Return what an independent reviewer gets for one cluster: locations and the change, never the judge's
    reasoning."""
    entry: dict[str, object] = {
        "cluster": record["cluster"],
        "digest": record["digest"],
        "kind": record["decision"],
        "locations": record["locations"],
        "inbound": record["inbound"],
        "guarded": record["guarded"],
    }
    if record["decision"] == "merge-prune":
        entry |= {
            "proposal": record["proposal"],
            "obligations": record["obligations"],
            "survivor": record["survivor"],
            "repoint": judged.get("repoint", []),
        }
    else:
        entry |= {
            "edits": judged["edits"],
            "repoint": judged.get("repoint", []),
            "crosses_files": record["crosses_files"],
        }
    return entry


def _pick_verdicts(reviews: list[dict[str, object]]) -> dict[str, dict[str, dict[str, object]]]:
    """Index review verdicts by cluster, then by the digest of the change reviewed; the most severe verdict wins."""
    picked: dict[str, dict[str, dict[str, object]]] = {}
    for review in reviews:
        cid, digest, verdict = (review.get(key) for key in ("cluster", "digest", "verdict"))
        evidence = review.get("evidence")
        if not all(isinstance(v, str) for v in (cid, digest, verdict)) or verdict not in _VERDICT_RANK:
            continue
        if verdict == "holds" and not (isinstance(evidence, str) and evidence.strip()):
            continue
        slot = picked.setdefault(cid, {})
        if _VERDICT_RANK[verdict] >= _VERDICT_RANK.get(str(slot.get(digest, {}).get("verdict")), -1):
            slot[digest] = review
    return picked


#: Longest ``lines`` value accepted on an edit; a longer one is not a line range.
_MAX_LINES_CHARS = 24


def _clean_edit(edit: object) -> tuple[dict[str, str], str]:
    """Return an edit reduced to its five text fields, or the reason it is not a well-formed edit."""
    if not isinstance(edit, dict):
        return {}, "an edit is not an object"
    clean: dict[str, str] = {}
    for key in ("file", "action", "before", "after"):
        value = edit.get(key, "")
        if not isinstance(value, str):
            return {}, f"edit field {key} is not text"
        clean[key] = value
    lines = edit.get("lines", "")
    lines = str(lines) if isinstance(lines, int) and not isinstance(lines, bool) else lines
    if not isinstance(lines, str) or len(lines) > _MAX_LINES_CHARS:
        return {}, "edit 'lines' is not a short range"
    return {**clean, "lines": lines}, ""


def _clean_obligation(entry: object) -> tuple[dict[str, object], str]:
    """Return an obligation entry reduced to its four fields, or the reason it is not well formed.

    A field that is present must have its type; a field that is absent is left for the merge rules to flag.
    """
    if not isinstance(entry, dict):
        return {}, "an obligation entry is not an object"
    clean: dict[str, object] = {}
    for key in OBLIGATION_FIELDS:
        if key in entry:
            if not isinstance(entry[key], str):
                return {}, f"obligation field {key} is not text"
            clean[key] = entry[key]
    if "modal_preserved" in entry:
        if not isinstance(entry["modal_preserved"], bool):
            return {}, "obligation field modal_preserved is not true or false"
        clean["modal_preserved"] = entry["modal_preserved"]
    return clean, ""


def normalize_judgment(raw: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    """Return a judge decision reduced to typed fields, and the reasons it is malformed.

    Every later step reads only this record, so a key the schema does not name (a rationale hidden in an edit, an extra
    obligation field, an object where a list belongs) can never reach a reviewer, a written file or a type error.
    """
    problems: list[str] = []

    def text(key: str) -> str:
        value = raw.get(key, "")
        if not isinstance(value, str):
            problems.append(f"{key} is not text")
            return ""
        return value

    def items(key: str) -> list[object]:
        value = raw.get(key, [])
        if not isinstance(value, list):
            problems.append(f"{key} is not a list")
            return []
        return value

    for key in ("shared", "distinct", "proposal"):
        text(key)
    triggers = raw.get("triggers", {})
    if not isinstance(triggers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in triggers.items()
    ):
        problems.append("triggers is not an object of text")
    repoint = items("repoint")
    if not all(isinstance(r, str) for r in repoint):
        problems.append("repoint holds a non-text entry")
    edits: list[dict[str, str]] = []
    for edit in items("edits"):
        clean, why = _clean_edit(edit)
        if why:
            problems.append(why)
        else:
            edits.append(clean)
    obligations: list[dict[str, object]] = []
    for entry in items("obligation_map"):
        cleaned, why = _clean_obligation(entry)
        if why:
            problems.append(why)
        else:
            obligations.append(cleaned)
    clean_judgment: dict[str, object] = {
        "cluster": text("cluster"),
        "decision": text("decision"),
        "survivor": text("survivor"),
        "reason": text("reason"),
        "repoint": [r for r in repoint if isinstance(r, str)],
        "obligation_map": obligations,
        "edits": edits,
    }
    return clean_judgment, list(dict.fromkeys(problems))


def _has_type(value: object, kind: type) -> bool:
    """Report whether ``value`` has type ``kind``; a bool is never an int."""
    return isinstance(value, kind) and not (kind is int and isinstance(value, bool))


def _shape_problems(record: object, spec: dict[str, type], where: str) -> list[str]:
    """Return what is wrong with ``record`` against a ``{field: type}`` spec: not an object, or a field missing or of
    another type."""
    if not isinstance(record, dict):
        return [f"{where} is not an object"]
    return [
        f"{where}.{key} is missing or not {kind.__name__}"
        for key, kind in spec.items()
        if not _has_type(record.get(key), kind)
    ]


def _text_list_problems(value: object, where: str) -> list[str]:
    """Return a problem unless ``value`` is a list holding only text."""
    ok = isinstance(value, list) and all(isinstance(entry, str) for entry in value)
    return [] if ok else [f"{where} is not a list of text"]


_SPAN_SPEC: dict[str, type] = {"id": str, "file": str, "section": str, "start_line": int, "end_line": int}
_RANGE_SPEC: dict[str, type] = {"id": str, "file": str, "start_line": int, "end_line": int}
_GUARD_SPEC: dict[str, type] = {"title": str, "line": int, "refs": list}
_VETTED_SPEC: dict[str, type] = {
    **{"id": str, "ok": bool, "judge": bool, "merge_allowed": bool, "tokens_before": int, "load_class": str},
    **{"items": list, "members": list, "locations": list, "blocked_ranges": list, "inbound": dict},
    **{"guarded": list, "file_guarded": list},
}
_VETTED_TOP_SPEC: dict[str, type] = {"schema_version": int, "floor": int, "max_judged": int}
_MAX_SHAPE_PROBLEMS = 5


def _json_clean(value: object) -> object:
    """Return ``value`` as JSON would round-trip it (tuples become lists), so it compares with a file's content."""
    return json.loads(json.dumps(value))


def _inventory_problems(inventory: object) -> list[str]:
    """Return why a document cannot even be rebuilt: wrong schema, or a root or kinds list of the wrong type."""
    if (
        not isinstance(inventory, dict)
        or not _has_type(inventory.get("schema_version"), int)
        or inventory["schema_version"] != SCHEMA_VERSION
    ):
        return [f"not a schema-{SCHEMA_VERSION} inventory"]
    kinds = inventory.get("kinds")
    if not isinstance(inventory.get("root"), str) or not isinstance(kinds, list) or not kinds:
        return ["root is not text or kinds is not a non-empty list"]
    if not all(isinstance(k, str) and k in ALL_KINDS for k in kinds):
        return ["kinds names something other than rules, skills or agents"]
    return []


def inventory_mismatch(inventory: dict[str, object]) -> list[str]:
    """Return which parts of an inventory differ from a fresh build of the tree it names, or an empty list.

    The inventory is derived data, so it is never trusted: the tree is inventoried again with the same root and kinds
    and the two documents are compared whole. An edited source, an added or removed instruction file, a changed
    propagation manifest, and a hand-edited item, group, load class or flag all show up as a difference.
    """
    root = Path(str(inventory["root"]))
    if not root.is_dir():
        return [f"root {root} is not a directory"]
    rebuilt = _json_clean(build_inventory(root.resolve(), tuple(inventory["kinds"])))  # type: ignore[arg-type]
    return [
        f"{key} differs from a rebuild of the tree"
        for key in sorted(set(rebuilt) | set(inventory))  # type: ignore[arg-type]
        if rebuilt.get(key) != inventory.get(key)  # type: ignore[attr-defined]
    ][:_MAX_SHAPE_PROBLEMS]


def _vetted_problems(vetted: object) -> list[str]:
    """Return why a document is not a usable validate report.

    A cluster that may be settled has an id that is safe as a file name, because the id names the proposal and
    obligation files in the run directory; a hand-edited absolute or ``..`` id must never reach a file operation.
    """
    if not isinstance(vetted, dict) or not isinstance(vetted.get("clusters"), list):
        return ["not a validate report (clusters is missing or not a list)"]
    present = {key: kind for key, kind in _VETTED_TOP_SPEC.items() if key in vetted}
    problems: list[str] = _shape_problems(vetted, present, "vetted")
    if not problems and vetted.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        return [f"not a schema-{SCHEMA_VERSION} validate report"]
    for n, cluster in enumerate(vetted["clusters"]):
        where = f"clusters[{n}]"
        found = _shape_problems(cluster, _VETTED_SPEC, where)
        if not found:
            if cluster["ok"] and not _CLUSTER_ID.fullmatch(cluster["id"]):
                found.append(f"{where}.id {cluster['id']!r} is not a safe name")
            found += _text_list_problems(cluster["members"], f"{where}.members")
            found += [p for s in cluster["locations"] for p in _shape_problems(s, _SPAN_SPEC, f"{where}.locations")]
            found += [
                p for s in cluster["blocked_ranges"] for p in _shape_problems(s, _RANGE_SPEC, f"{where}.blocked_ranges")
            ]
            for guard in cluster["guarded"]:
                found += _shape_problems(guard, {**_GUARD_SPEC, "id": str, "file": str}, f"{where}.guarded")
                found += _text_list_problems(
                    guard.get("refs") if isinstance(guard, dict) else None, f"{where}.guarded.refs"
                )
            for refs in cluster["inbound"].values():
                found += _text_list_problems(refs, f"{where}.inbound")
        problems += found
    return problems[:_MAX_SHAPE_PROBLEMS]


def vetted_mismatch(inventory: dict[str, object], vetted: dict[str, object]) -> list[str]:
    """Return where a validate report differs from running validate again on its own clusters, or an empty list.

    The cluster ids and member lists are the only model-written input; everything else (locations, citing files, guarded
    headings, whether merging is allowed, the load class, which clusters are judged and why the others are not) is
    recomputed with the floor and cap the report recorded, so no hand-edited flag can change what is settled.
    """
    clusters = vetted.get("clusters")
    floor, cap = vetted.get("floor"), vetted.get("max_judged")
    shaped = (
        isinstance(clusters, list)
        and _has_type(floor, int)
        and _has_type(cap, int)
        and all(isinstance(c, dict) and isinstance(c.get("items"), list) for c in clusters)
    )
    if not shaped:
        return ["floor, max_judged or a cluster's items are missing or of the wrong type"]
    again = validate(inventory, [{"id": c.get("id"), "items": c["items"]} for c in clusters], floor, cap)  # type: ignore[arg-type,index,union-attr]
    expected = _json_clean(again["clusters"])
    return [
        f"clusters[{n}] differs from validate's result for the same clusters"
        for n, (fresh, given) in enumerate(zip(expected, clusters))  # type: ignore[arg-type,call-overload]
        if fresh != given
    ][:_MAX_SHAPE_PROBLEMS]


def _not_settled(cid: str, decision: str, reason: str) -> dict[str, object]:
    """Return the record of a judgment or cluster that could not be settled."""
    return {"cluster": cid, "decision": decision, "validation": "not-required", "downgrades": [reason], "savings": 0}


def settle(
    vetted: dict[str, object],
    judgments: list[dict[str, object]],
    proposals: Path,
    reviews: list[dict[str, object]],
    root: Path | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Settle every judge decision; return the settled document and the reviewer inputs still outstanding.

    Judgments correspond one-to-one with the clusters marked ``judge``: a duplicate, an ineligible or an unknown
    judgment is recorded as rejected, and a judged cluster with no judgment is recorded as unjudged.
    """
    if problems := _vetted_problems(vetted):
        raise ValueError("unusable validate report: " + "; ".join(problems))
    valid = {str(v["id"]): v for v in vetted["clusters"] if v["ok"]}  # type: ignore[attr-defined]
    verdicts = _pick_verdicts(reviews)
    records: list[dict[str, object]] = []
    pending: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in judgments:
        judged, malformed = normalize_judgment(raw)
        cid = str(judged["cluster"])
        vet = valid.get(cid)
        if vet is None:
            problem = "cluster failed the guard or is unknown"
        elif cid in seen:
            problem = "duplicate judgment for this cluster"
        elif not vet["judge"]:
            problem = f"cluster was not eligible for judging ({vet.get('skip_reason', 'not marked judge')})"
        elif malformed:
            problem = "malformed judgment: " + "; ".join(malformed)
        else:
            problem = ""
        seen.add(cid)
        if problem:
            records.append(_not_settled(cid, "rejected", problem))
            continue
        record = settle_cluster(judged, vet, proposals, verdicts.get(cid), _read_sources(root, vet), root)  # type: ignore[arg-type]
        records.append(record)
        if record["needs_review"] and record["validation"] == "unvalidated":
            pending.append(_review_input(record, judged))
    records += [
        _not_settled(cid, "unjudged", "no judgment returned for a judged cluster")
        for cid, vet in valid.items()
        if vet["judge"] and cid not in seen
    ]
    return {"schema_version": SCHEMA_VERSION, "clusters": records, "totals": _totals(records)}, pending


def _totals(records: list[dict[str, object]]) -> dict[str, object]:
    """Sum validated and unvalidated merge-prune savings per load class, and count decisions."""
    totals: dict[str, object] = {"decisions": {}, "savings": {}}
    for record in records:
        decisions: dict[str, int] = totals["decisions"]  # type: ignore[assignment]
        key = f"{record['decision']}/{record['validation']}"
        decisions[key] = decisions.get(key, 0) + 1
        if record["decision"] == "merge-prune":
            savings: dict[str, dict[str, int]] = totals["savings"]  # type: ignore[assignment]
            bucket = savings.setdefault(str(record["load_class"]), {"validated": 0, "unvalidated": 0})
            bucket["validated" if record["validation"] == "holds" else "unvalidated"] += int(record["savings"])  # type: ignore[call-overload]
    return totals


def _reject_constant(name: str) -> None:
    """Refuse ``NaN`` and ``Infinity``, which JSON does not define, so no artifact carries a nonstandard number."""
    raise ValueError(f"{name} is not a JSON value")


def _finite_float(text: str) -> float:
    """Parse a JSON fraction or exponent, refusing one too large for a double (``1e999`` would read as infinity)."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is out of range")
    return value


def _load_json(path: str) -> object:
    """Read a JSON file, exiting 1 with a message when it is missing or malformed."""
    try:
        return json.loads(
            Path(path).read_text(encoding="utf-8"), parse_constant=_reject_constant, parse_float=_finite_float
        )
    except (OSError, ValueError, RecursionError) as exc:
        print(f"! BLOCKED — cannot read {path}: {exc}")
        raise SystemExit(1) from exc


def _load_list(path: str, what: str) -> list[dict[str, object]]:
    """Read a JSON list of objects, exiting 1 when the file holds anything else."""
    data = _load_json(path)
    if not isinstance(data, list):
        print(f"! BLOCKED — {path} must hold a JSON list of {what}")
        raise SystemExit(1)
    for position, entry in enumerate(data):
        if not isinstance(entry, dict):
            print(f"! BLOCKED — {path} entry {position} is not a JSON object (it is {type(entry).__name__})")
            raise SystemExit(1)
    return data


def _load_inventory(path: str) -> dict[str, object]:
    """Read an inventory, exiting 1 unless it is a schema-current document."""
    inventory = _load_json(path)
    if problems := _inventory_problems(inventory):
        print(f"! BLOCKED — {path} is not a usable inventory: {'; '.join(problems)} — re-run inventory")
        raise SystemExit(1)
    if problems := inventory_mismatch(inventory):  # type: ignore[arg-type]
        print(f"! BLOCKED — {path} does not match the tree: {'; '.join(problems)} — re-run inventory")
        raise SystemExit(1)
    return inventory  # type: ignore[return-value]


#: Top-level directories of the tree whose files are instruction sources; this mode never writes into them.
_SOURCE_TOPS = ("plugins", ".claude", ".codex")


def _under(path: Path, parent: Path) -> bool:
    """Report whether ``path`` lies inside ``parent`` once both are resolved (symlinks followed)."""
    return path.resolve().is_relative_to(parent.resolve())


def _allowed_output(path: Path, root: Path, run: Path | None = None) -> bool:
    """Report whether this mode may write ``path``: a run artifact, and never an instruction source.

    Allowed: inside ``<root>/.temp`` or ``<root>/.reports``, or inside the system temp directory when that is not
    inside ``root``. Everything else, the instruction sources and any other file of the tree or of the machine included,
    is refused. With ``run`` (the directory of the run's inventory) the path must also lie inside that directory, so a
    later step never writes into another run's artifacts.
    """
    if run is not None and not _under(path, run):
        return False
    if any(_under(path, root / top) for top in _SOURCE_TOPS):
        return False
    if any(_under(path, root / top) for top in (".temp", ".reports")):
        return True
    return _under(path, Path(tempfile.gettempdir())) and not _under(path, root)


def _write_artifact(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` by replacing the name, never writing through a symlink or hard link standing there.

    The text goes to a fresh staging file next to ``path`` (created exclusively, links not followed) and replaces the
    name atomically, so another file sharing the old inode or reached by a link keeps its content.
    """
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staging.unlink(missing_ok=True)
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(staging, path)


def _emit(document: object, out: str, root: Path, run: Path | None = None) -> None:
    """Write ``document`` to ``out`` when given, else print it as JSON."""
    text = json.dumps(document, indent=2, sort_keys=True, allow_nan=False)
    if out:
        if not _allowed_output(Path(out), root, run):
            print(f"! BLOCKED — {out} is not a run artifact location (.temp/, .reports/ or the system temp directory)")
            raise SystemExit(1)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        _write_artifact(Path(out), text + "\n")
    else:
        print(text)


def _print_inventory_summary(document: dict[str, object], out: str) -> None:
    """Print the one-line-per-hierarchy summary of an inventory run."""
    for hierarchy, bucket in sorted(document["summary"].items()):  # type: ignore[attr-defined]
        print(f"{hierarchy}: {bucket['items']} sections, ~{bucket['tokens']} tokens, {bucket['blocked']} blocked")
    if out:
        print(f"inventory={out}")


def _build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the six subcommands."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("inventory", help="inventory instruction sections")
    inv.add_argument("--root", default=".")
    inv.add_argument("--kinds", default=",".join(ALL_KINDS))
    inv.add_argument("--out", default="")
    sel = sub.add_parser("select", help="choose the groups to examine")
    sel.add_argument("--inventory", required=True)
    sel.add_argument("--scope", default="")
    sel.add_argument("--min-tokens", type=int, default=1500)
    sel.add_argument("--max-groups", type=int, default=12)
    sel.add_argument("--batches", type=int, default=4)
    sel.add_argument("--out", default="")
    val = sub.add_parser("validate", help="vet proposed clusters")
    val.add_argument("--inventory", required=True)
    val.add_argument("--clusters", required=True)
    val.add_argument("--min-cluster-tokens", type=int, default=MIN_CLUSTER_TOKENS)
    val.add_argument("--max-judged", type=int, default=MAX_JUDGED)
    val.add_argument("--out", default="")
    stl = sub.add_parser("settle", help="apply the deterministic rules to judge decisions")
    stl.add_argument("--inventory", required=True)
    stl.add_argument("--vetted", required=True)
    stl.add_argument("--judgments", required=True)
    stl.add_argument("--proposals-dir", required=True)
    stl.add_argument("--reviews", default="")
    stl.add_argument("--review-inputs", default="")
    stl.add_argument("--out", default="")
    collect = sub.add_parser("collect", help="join the JSON lists written by parallel agents into one list")
    collect.add_argument("--root", default=".")
    collect.add_argument("--dir", required=True)
    collect.add_argument("--glob", required=True)
    collect.add_argument("--out", required=True)
    stash = sub.add_parser("stash", help="save one line of run state under a run artifact location")
    stash.add_argument("--root", default=".")
    stash.add_argument("--path", required=True)
    stash.add_argument("--text", required=True)
    return parser


def _run_dir(inventory_path: str) -> Path:
    """Return the directory holding the run's inventory: the only place the run's later steps may write."""
    return Path(inventory_path).resolve().parent


def _tolerate_any_text() -> None:
    """Make printing never raise on model-written text a terminal cannot encode (a lone surrogate, say)."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(errors="backslashreplace")


def _run_collect(args: argparse.Namespace) -> int:
    """Run the collect subcommand: join the JSON lists of the files a pattern names into one list."""
    root = Path(args.root).resolve()
    folder = Path(args.dir).resolve()
    if not _allowed_output(Path(args.out), root, folder):
        print(f"! BLOCKED — {args.out} is not a run artifact location inside {args.dir}")
        return 1
    joined: list[dict[str, object]] = []
    for path in sorted(folder.glob(args.glob)):
        if path.is_file() and not path.is_symlink():
            joined += _load_list(str(path), "objects")
    _emit(joined, args.out, root, folder)
    return 0


def _run_stash(args: argparse.Namespace) -> int:
    """Run the stash subcommand: save one line of run state where a later shell block reads it back."""
    if not _allowed_output(Path(args.path), Path(args.root).resolve()):
        print(
            f"! BLOCKED — {args.path} is not a run artifact location (.temp/, .reports/ or the system temp directory)"
        )
        return 1
    Path(args.path).parent.mkdir(parents=True, exist_ok=True)
    _write_artifact(Path(args.path), args.text + "\n")
    return 0


def _run_settle(args: argparse.Namespace) -> int:
    """Run the settle subcommand."""
    inventory = _load_inventory(args.inventory)
    vetted = _load_json(args.vetted)
    problems = _vetted_problems(vetted) or vetted_mismatch(inventory, vetted)  # type: ignore[arg-type]
    if problems:
        print(f"! BLOCKED — {args.vetted} is not a usable validate report: {'; '.join(problems)} — re-run validate")
        return 1
    root = Path(str(inventory["root"]))
    run = _run_dir(args.inventory)
    if not _allowed_output(Path(args.proposals_dir), root, run):
        print(f"! BLOCKED — {args.proposals_dir} is not a run artifact location; use the run directory")
        return 1
    judgments = _load_list(args.judgments, "judge decisions")
    reviews = _load_list(args.reviews, "review verdicts") if args.reviews else []
    document, pending = settle(vetted, judgments, Path(args.proposals_dir), reviews, root)  # type: ignore[arg-type]
    _emit(document, args.out, root, run)
    if args.review_inputs:
        _emit(pending, args.review_inputs, root, run)
    for record in document["clusters"]:  # type: ignore[attr-defined]
        print(f"{record['cluster']}: {record['decision']} ({record['validation']})")
    print(f"awaiting review: {len(pending)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the inventory, select, validate, settle, collect or stash subcommand."""
    args = _build_parser().parse_args(argv)
    _tolerate_any_text()
    if args.command == "stash":
        return _run_stash(args)
    if args.command == "collect":
        return _run_collect(args)
    if args.command == "inventory":
        kinds = tuple(k for k in args.kinds.split(",") if k in ALL_KINDS)
        if not kinds:
            print(f"! BLOCKED — --kinds must name one of: {', '.join(ALL_KINDS)}")
            return 1
        document = build_inventory(Path(args.root).resolve(), kinds)
        _emit(document, args.out, Path(args.root))
        _print_inventory_summary(document, args.out)
        return 0
    if args.command == "settle":
        return _run_settle(args)
    inventory = _load_inventory(args.inventory)
    if args.command == "select":
        chosen = select_groups(inventory, args.scope, args.min_tokens, args.max_groups, args.batches)
        if chosen["unmatched_tokens"]:
            print(f"! BLOCKED — scope token(s) name no kind, plugin, agent or skill: {chosen['unmatched_tokens']}")
            return 1
        _emit(chosen, args.out, Path(str(inventory["root"])), _run_dir(args.inventory))
        print(
            f"selected {len(chosen['selected'])} groups in {len(chosen['batches'])} batches; "  # type: ignore[arg-type]
            f"{len(chosen['not_examined'])} not examined; unmatched scope tokens: {chosen['unmatched_tokens']}"
        )  # type: ignore[arg-type]
        return 0
    clusters = _load_list(args.clusters, "clusters")
    report = validate(inventory, clusters, args.min_cluster_tokens, args.max_judged)
    _emit(report, args.out, Path(str(inventory["root"])), _run_dir(args.inventory))
    for cluster in report["clusters"]:  # type: ignore[attr-defined]
        verdict = "ok" if cluster["ok"] else "NOT ok: " + "; ".join(cluster["reasons"])
        print(f"{cluster['id']}: {verdict} (~{cluster['tokens_before']} tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
