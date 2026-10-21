#!/usr/bin/env python
"""check_bash_persistence.py — Detect shell variables referenced across Bash tool call boundaries.

Variables assigned in one ```bash block are NOT available in later bash blocks —
each Bash tool call runs in a fresh shell. Cross-block references silently expand
to empty string, corrupting paths, conditionals, and spawn prompts without any
error message.

False-positive suppression — a cross-block reference is NOT reported when the
referencing block already recovers the value locally:

1. Re-derivation: the block re-loads persisted state before the reference via
   ``eval "$(...)"`` or ``source``/``.`` sourcing (state-file reload pattern),
   at line start or after ``&&``/``||``/``;``/``then``/``do`` (e.g.
   ``[ -f "$F" ] && . "$F"``). Re-assignment in the same block is likewise
   safe: ``VAR=...``, the sentinel read-back ``IFS= read -r VAR < file``,
   ``mapfile``/``readarray`` targets and ``for VAR in`` loop variables.
2. Empty-var-defended: every reference is a parameter expansion with a default
   (``${VAR:-x}``) or feeds a stripped var guarded by ``[ -z "$X" ] && X=...``.

Exemption is per variable, never per block. A token no block assigns (an
orchestrator-filled ``${I}``, an ``<identifier>`` placeholder) is never
reported itself, but it no longer hides other lost variables in the same
block — the old whole-block template exemption let the standard
``export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"`` preamble mask nearly
every block.

Text the shell never expands is ignored before scanning: single-quoted spans
(e.g. jq programs carrying ``$cmd``) and ``#`` comments, full-line or trailing.
Known limitation: heredoc bodies are lexed like ordinary shell, so an
apostrophe inside an unquoted heredoc body would open a false single-quoted
span; no scanned block has that shape.

Scope of ``--scan-dir``: every Markdown file under a plugin's ``skills/`` tree (``SKILL.md`` plus the ``modes/``,
``_shared/`` and ``templates/`` files it loads), each ``agents/*.md`` prompt and ``claude-skills/``. Their blocks run as
separate Bash calls just like the entry point's. Explicit file arguments are checked whatever their path.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_bash_persistence.py" [files...]
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_bash_persistence.py" --scan-dir plugins/

Output (stdout):
    One finding line per violation with prefix "C41-CRITICAL:", or a single pass line.

Exit codes:
    0   all files pass
    1   one or more violations found
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Variables that persist across bash calls (env vars, skill runner context, common loop vars)
_SKIP_VARS: frozenset[str] = frozenset(
    {
        "HOME",
        "PATH",
        "USER",
        "SHELL",
        "TMPDIR",
        "PWD",
        "OLDPWD",
        "IFS",
        "PIPESTATUS",
        "BASH_VERSION",
        "SECONDS",
        "RANDOM",
        "UID",
        "EUID",
        "ARGUMENTS",
        "CLAUDE_PLUGIN_ROOT",
        "LANG",
        "LC_ALL",
        "TERM",
        "LOCAL_MODE",
        "AUDIT_TPL",
        "RUN_DIR",
        "REPORT_DIR",
        "SKIP_GATE",
        # session-token sources of the standard CSID preamble; set by the shell/harness, never by a block
        "PPID",
        "CLAUDE_CODE_SESSION_ID",
        # common short loop/counter vars unlikely to cause real persistence bugs
        "f",
        "i",
        "j",
        "k",
        "n",
        "b",
        "s",
        "v",
        "c",
        "dir",
        "file",
        "found",
        "var",
        "out",
        "err",
        "rc",
        "ret",
        "line",
        "name",
        "key",
        "val",
        "base",
        "dest",
        "src",
        "arg",
    }
)

_MIN_BLOCKS = 2  # need ≥2 blocks for a cross-block reference to be possible
#: Globs, relative to a scan root, of the Markdown files whose ```bash blocks run as separate Bash calls.
_SCAN_GLOBS: tuple[str, ...] = ("*/skills/**/*.md", "*/agents/*.md", "*/claude-skills/**/*.md")
#: Matches the opening fence of a ``bash`` code block.
_BASH_OPEN = re.compile(r"^```bash\s*$")
#: Matches a bare closing code fence.
_FENCE_CLOSE = re.compile(r"^```\s*$")
#: Matches a shell variable assignment at line start, optionally prefixed by ``export`` or ``local``.
_ASSIGN = re.compile(r"^[ \t]*(?:export[ \t]+|local[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=")
#: Matches an assignment after a command separator, case-branch ``)``, brace or list keyword
#: (``A=1; B=2``, ``read -r X || X=""``, ``SINCE) SINCE=$V;;``, ``then X=1``).
_ASSIGN_INLINE = re.compile(
    r"(?:[;)&|{]|\bthen\b|\bdo\b|\belse\b)[ \t]*(?:export[ \t]+|local[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)="
)
#: Matches a ``$VAR`` or ``${VAR}`` reference with a name of at least two characters.
_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]+)\}?")
#: State-reload commands that re-derive prior shell state within a fresh block, at line start or after a
#: command separator / list keyword. The `.`/`source` path may be quoted (`. "$FILE"`) or bare (`. $FILE`, `. ./f`).
_RELOAD = re.compile(r"""(?:^|&&|\|\||;|\bthen\b|\bdo\b)[ \t]*(?:eval[ \t]+"?\$\(|source[ \t]+|\.[ \t]+["'./~$])""")
#: Shell lexemes relevant to expansion: backslash escapes and double-quoted strings are kept verbatim (they expand);
#: single-quoted spans and ``#`` comments (``#`` at line start or after whitespace) never expand and get blanked.
_LEX = re.compile(r"""(?P<keep>\\.|"(?:[^"\\]|\\.)*")|(?P<blank>'[^']*'|(?<!\S)\#[^\n]*)""", re.S)
#: A ``read``/``mapfile``/``readarray`` command word and its arguments up to the next command separator.
_READ_CMD = re.compile(r"(?:^|(?<=[\s;|&(]))(read|mapfile|readarray)[ \t]+([^;|&\n]*)")
#: A ``for VAR in`` loop header.
_FOR_VAR = re.compile(r"\bfor[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]+in\b")
#: Option letters that consume the next word as their argument, per builtin (``read -t`` takes a timeout,
#: ``mapfile -t`` takes none).
_ARG_FLAGS: dict[str, frozenset[str]] = {
    "read": frozenset("adinNptu"),
    "mapfile": frozenset("dnOsuCc"),
    "readarray": frozenset("dnOsuCc"),
}
#: A bare shell identifier.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def extract_bash_blocks(text: str) -> list[str]:
    """Return bash fenced block bodies in document order.

    Args:
        text: full content of a SKILL.md file.

    Returns:
        List of block body strings, one per ```bash...``` fence.

    Examples:
        >>> extract_bash_blocks("```bash\\nFOO=1\\n```\\n")
        ['FOO=1\\n']
        >>> extract_bash_blocks("no fences here")
        []
        >>> len(extract_bash_blocks("```bash\\nA=1\\n```\\n```bash\\nB=2\\n```\\n"))
        2
    """
    blocks: list[str] = []
    buf: list[str] = []
    in_bash = False
    for line in text.splitlines(keepends=True):
        if not in_bash and _BASH_OPEN.match(line):
            in_bash = True
            buf = []
        elif in_bash and _FENCE_CLOSE.match(line):
            in_bash = False
            blocks.append("".join(buf))
        elif in_bash:
            buf.append(line)
    return blocks


def strip_unexpanded(block: str) -> str:
    """Blank out the parts of a bash block the shell never expands, keeping line structure.

    Single-quoted spans and ``#`` comments are replaced by spaces (newlines kept, so
    line indices stay aligned with the original block). Double-quoted strings and
    backslash escapes are kept verbatim — an apostrophe inside ``"..."`` is not a
    quote opener.

    Args:
        block: bash block body text.

    Returns:
        Block text with unexpanded spans blanked.

    Examples:
        >>> strip_unexpanded("jq '$cmd' \\"$F\\"\\n").split()
        ['jq', '"$F"']
        >>> "$X" in strip_unexpanded('echo "it\\'s $X"\\n')
        True
        >>> strip_unexpanded("echo $A  # don't\\necho $B\\n").splitlines()[1]
        'echo $B'
    """

    def _sub(m: re.Match[str]) -> str:
        if m.group("keep") is not None:
            return m.group(0)
        return re.sub(r"[^\n]", " ", m.group(0))

    return _LEX.sub(_sub, block)


def _read_targets(line: str) -> set[str]:
    """Return variable names a ``read``/``mapfile``/``readarray`` command or ``for`` header assigns on one line.

    Option words are skipped; options taking an argument consume the next word
    (``read -a NAME`` makes ``NAME`` a target). Collection stops at the first
    word that is not an identifier (a redirection, a quote, a separator).

    Args:
        line: single line of bash, already passed through strip_unexpanded.

    Returns:
        Set of assigned names.

    Examples:
        >>> sorted(_read_targets('IFS= read -r RUN_DIR < "$F" 2>/dev/null || RUN_DIR=""'))
        ['RUN_DIR']
        >>> sorted(_read_targets("while IFS=$'\\\\t' read -r A B; do"))
        ['A', 'B']
        >>> sorted(_read_targets("mapfile -t FILES < <(ls)"))
        ['FILES']
        >>> sorted(_read_targets("read -t 5 -a ARR"))
        ['ARR']
        >>> sorted(_read_targets("for P in $LIST; do"))
        ['P']
    """
    names = set(_FOR_VAR.findall(line))
    for m in _READ_CMD.finditer(line):
        arg_flags = _ARG_FLAGS[m.group(1)]
        words = iter(m.group(2).split())
        for word in words:
            if word.startswith("-") and len(word) > 1:
                if word[-1] in arg_flags:
                    nxt = next(words, "")
                    if word[-1] == "a" and m.group(1) == "read" and _IDENT.fullmatch(nxt):
                        names.add(nxt)
                continue
            if not _IDENT.fullmatch(word):
                break
            names.add(word)
    return names


def assigned_vars(block: str) -> frozenset[str]:
    """Return variable names assigned in a bash block.

    Matches bare assignments at the start of a line (after optional whitespace
    and optional export/local prefix) or after a separator / case-branch ``)`` /
    list keyword, plus ``read``/``mapfile``/``readarray`` targets and
    ``for VAR in`` loop variables anywhere on a line. Comment lines are skipped.

    Args:
        block: bash block body text.

    Returns:
        Frozenset of assigned variable names (no $ prefix).

    Examples:
        >>> sorted(assigned_vars("FOO=1\\nBAR=$(cmd)\\n  BAZ=x\\n"))
        ['BAR', 'BAZ', 'FOO']
        >>> assigned_vars("# FOO=1\\n")
        frozenset()
        >>> sorted(assigned_vars("export QUX=y\\nlocal QUZ=z\\n"))
        ['QUX', 'QUZ']
        >>> sorted(assigned_vars('IFS= read -r PYTEST_CMD < "$S" || PYTEST_CMD=""\\n'))
        ['IFS', 'PYTEST_CMD']
        >>> sorted(assigned_vars('A=1; B=2\\ncase "$K" in SINCE) SINCE=$V;; esac\\n'))
        ['A', 'B', 'SINCE']
    """
    names: set[str] = set()
    for line in block.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ASSIGN.match(line)
        if m:
            names.add(m.group(1))
        names.update(_ASSIGN_INLINE.findall(line))
        names |= _read_targets(line)
    return frozenset(names)


def referenced_vars(block: str) -> frozenset[str]:
    """Return variable names referenced via $VAR or ${VAR} in a bash block.

    Filters out known-safe env vars, single-char names, and pure-digit refs.
    Full-line ``#`` comments are skipped — a var named only in a comment is not
    executed shell.

    Args:
        block: bash block body text.

    Returns:
        Frozenset of referenced variable names (no $ prefix).

    Examples:
        >>> sorted(referenced_vars("echo $FOO ${BAR_BAZ}\\n"))
        ['BAR_BAZ', 'FOO']
        >>> referenced_vars("echo $HOME $1\\n")
        frozenset()
        >>> referenced_vars("# echo $FOO\\n")
        frozenset()
        >>> referenced_vars("")
        frozenset()
    """
    names: set[str] = set()
    for line in block.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in _REF.finditer(line):
            v = m.group(1)
            if v in _SKIP_VARS or len(v) <= 1:
                continue
            names.add(v)
    return frozenset(names)


def _refs_var(line: str, var: str) -> bool:
    """Return True if line references $var / ${var} (whole-name match).

    Args:
        line: single line of bash.
        var: variable name (no $).

    Returns:
        True when the exact variable is referenced on the line.

    Examples:
        >>> _refs_var('echo "${FOO%/x}"', "FOO")
        True
        >>> _refs_var("echo $FOOBAR", "FOO")
        False
    """
    return re.search(r"\$\{?" + re.escape(var) + r"(?![A-Za-z0-9_])", line) is not None


def _ref_line_indices(block: str, var: str) -> list[int]:
    """Return indices of non-comment lines in block that reference var.

    Args:
        block: bash block body.
        var: variable name (no $).

    Returns:
        Sorted list of 0-based line indices.

    Examples:
        >>> _ref_line_indices("A=1\\necho $A\\n# echo $A\\n", "A")
        [1]
    """
    lines = block.splitlines()
    return [i for i, ln in enumerate(lines) if not ln.lstrip().startswith("#") and _refs_var(ln, var)]


def reloads_before_ref(block: str, var: str) -> bool:
    """Return True if block re-loads persisted state before first reference to var.

    Detects ``eval "$(...)"`` and ``source``/``.`` sourcing on lines above the
    first reference, at line start or after a separator such as
    ``[ -f "$F" ] && . "$F"`` — the state-file reload / re-derivation pattern.

    Args:
        block: referencing bash block body.
        var: variable name (no $).

    Returns:
        True when a reload command precedes the first reference.

    Examples:
        >>> reloads_before_ref('eval "$(gen)"\\nrm "$S"\\n', "S")
        True
        >>> reloads_before_ref('rm "$S"\\neval "$(gen)"\\n', "S")
        False
        >>> reloads_before_ref('[ -f "$F" ] && . "$F"\\necho "$MODE"\\n', "MODE")
        True
    """
    idxs = _ref_line_indices(block, var)
    if not idxs:
        return False
    lines = block.splitlines()
    return any(_RELOAD.search(lines[i]) for i in range(idxs[0]))


def _defended_at(lines: list[str], i: int, var: str) -> bool:
    """Return True if the reference to var on line i is empty-var-defended.

    Defended forms: a default parameter expansion ``${var:-x}`` / ``${var:=x}`` /
    ``${var:?}`` / ``${var:+}``; or a stripped assignment ``NEW="${var%...}"``
    followed within 2 lines by a ``[ -z "$NEW" ]`` fallback guard.

    Args:
        lines: all block lines.
        i: index of the referencing line.
        var: variable name (no $).

    Returns:
        True when the occurrence is defended against an empty value.
    """
    line = lines[i]
    if re.search(r"\$\{" + re.escape(var) + r":[-=?+]", line):
        return True
    strip = re.search(r"\$\{" + re.escape(var) + r"[%#/^,]", line)
    assign = _ASSIGN.match(line)
    if not (strip and assign):
        return False
    new = assign.group(1)
    guard = re.compile(r"-z[ \t]+\"?\$\{?" + re.escape(new) + r"\}?\"?")
    return any(guard.search(lines[k]) for k in range(i + 1, min(i + 3, len(lines))))


def refs_all_defended(block: str, var: str) -> bool:
    """Return True when every reference to var in block is empty-var-defended.

    Args:
        block: referencing bash block body.
        var: variable name (no $).

    Returns:
        True if all references survive an empty value (see _defended_at).

    Examples:
        >>> refs_all_defended('B="${A%/x}"\\n[ -z "$B" ] && B=y\\n', "A")
        True
        >>> refs_all_defended("echo $A\\n", "A")
        False
    """
    idxs = _ref_line_indices(block, var)
    if not idxs:
        return False
    lines = block.splitlines()
    return all(_defended_at(lines, i, var) for i in idxs)


def scan_paths(root: Path) -> list[Path]:
    """Return every Markdown file under ``root`` whose bash blocks run as separate Bash calls.

    That is a skill's ``SKILL.md`` and everything it loads (``modes/``, ``_shared/``, ``templates/``), each agent
    prompt, and Claude-side bridge skills. A mode file's blocks are executed as their own Bash calls exactly like
    the entry point's, so the same fresh-shell loss applies to them.

    Args:
        root: Directory searched recursively.

    Returns:
        Matching paths, possibly with duplicates when patterns overlap (callers de-duplicate).
    """
    return [path for pattern in _SCAN_GLOBS for path in root.rglob(pattern)]


def check_file(path: Path) -> list[str]:
    """Return C41 violation strings for one Markdown file.

    Args:
        path: path to the skill, mode, template or agent Markdown file to analyse.

    Returns:
        List of finding strings; empty list = no violations.

    Examples:
        >>> import os, tempfile
        >>> content = "```bash\\nFOO=bar\\n```\\n```bash\\necho $FOO\\n```\\n"
        >>> with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
        ...     _ = f.write(content); name = f.name
        >>> findings = check_file(Path(name)); os.unlink(name)
        >>> any("C41" in x and "FOO" in x for x in findings)
        True
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    blocks = [strip_unexpanded(b) for b in extract_bash_blocks(text)]
    if len(blocks) < _MIN_BLOCKS:
        return []
    assigns = [assigned_vars(b) for b in blocks]
    findings: list[str] = []
    for i in range(1, len(blocks)):
        block = blocks[i]
        for var in sorted(referenced_vars(block)):
            if var in assigns[i]:  # re-assigned directly in same block
                continue
            src = next((j for j in range(i) if var in assigns[j]), None)
            if src is None:
                continue
            if reloads_before_ref(block, var):  # rule 1: eval/source state reload
                continue
            if refs_all_defended(block, var):  # rule 2: empty-var-defended
                continue
            findings.append(
                f"C41-CRITICAL: {path}: ${var} assigned in bash block {src + 1},"
                f" referenced in block {i + 1} — variable lost across Bash calls"
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — scan files and print findings.

    Args:
        argv: argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code: 0 = all pass, 1 = violations found.
    """
    parser = argparse.ArgumentParser(description="Detect shell variable persistence issues in skill and agent Markdown")
    parser.add_argument("files", nargs="*", help="Markdown files to check")
    parser.add_argument(
        "--scan-dir", metavar="DIR", help="recursively scan DIR for skill (incl. modes/_shared/templates) and agent .md"
    )
    parser.add_argument("--timeout", type=int, help="ignored — accepted for CLI compatibility")
    args = parser.parse_args(argv)

    paths: list[Path] = []
    if args.scan_dir:
        paths.extend(scan_paths(Path(args.scan_dir)))
    paths.extend(Path(f) for f in args.files)
    if not paths and not args.scan_dir:  # an explicit --scan-dir with no matches must not fall back to scanning cwd
        paths.extend(scan_paths(Path(".")))

    all_findings: list[str] = []
    for p in sorted(set(paths)):
        all_findings.extend(check_file(p))

    if all_findings:
        print("\n".join(all_findings))
        return 1
    print("✓: Check 41 — no cross-bash-call variable persistence issues found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
