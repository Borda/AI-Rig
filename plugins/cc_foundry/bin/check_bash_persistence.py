#!/usr/bin/env python
"""check_bash_persistence.py — Detect shell variables referenced across Bash tool call boundaries.

Variables assigned in one ```bash block are NOT available in later bash blocks —
each Bash tool call runs in a fresh shell. Cross-block references silently expand
to empty string, corrupting paths, conditionals, and spawn prompts without any
error message.

False-positive suppression — a cross-block reference is NOT reported when the
referencing block already recovers the value locally:

1. Re-derivation: the block re-loads persisted state before the reference via
   ``eval "$(...)"`` or ``source``/``.`` sourcing (state-file reload pattern).
   Direct ``VAR=...`` re-assignment in the same block is likewise safe.
2. Empty-var-defended: every reference is a parameter expansion with a default
   (``${VAR:-x}``) or feeds a stripped var guarded by ``[ -z "$X" ] && X=...``.
3. Template placeholder: the block also contains a ``$X``/``${X}`` token never
   assigned in ANY bash block (e.g. loop counter ``${I}``) — the block is an
   orchestrator-filled template, not verbatim-executed shell.

References inside full-line ``#`` comments are ignored (not executed shell).

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
#: Matches the opening fence of a ``bash`` code block.
_BASH_OPEN = re.compile(r"^```bash\s*$")
#: Matches a bare closing code fence.
_FENCE_CLOSE = re.compile(r"^```\s*$")
#: Matches a shell variable assignment at line start, optionally prefixed by ``export`` or ``local``.
_ASSIGN = re.compile(r"^[ \t]*(?:export[ \t]+|local[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=")
#: Matches a ``$VAR`` or ``${VAR}`` reference with a name of at least two characters.
_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]+)\}?")
#: Single-char-permitting reference regex — used only for template-placeholder detection (e.g. ${I}).
_REF_ANY = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
#: State-reload commands that re-derive prior shell state within a fresh block.
#: The `.`/`source` path may be quoted (`. "$FILE"`) or bare (`. $FILE`, `. ./f`).
_RELOAD = re.compile(r"""^[ \t]*(?:eval[ \t]+"?\$\(|source[ \t]+|\.[ \t]+["'./~$])""")
#: Angle-bracket identifier placeholders (e.g. <TARGET_MODULE>, <file>) mark a block
#: as a usage-example/doc snippet, not verbatim-executed shell. Matches only
#: <identifier> with no interior spaces — never shell redirection (`< file`),
#: process substitution (`<(`), or heredocs (`<<`).
_ANGLE_PLACEHOLDER = re.compile(r"<[A-Za-z_][A-Za-z0-9_]*>")


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


def assigned_vars(block: str) -> frozenset[str]:
    """Return variable names assigned in a bash block.

    Only matches bare assignments at the start of a line (after optional
    whitespace and optional export/local prefix). Comment lines are skipped.

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
    """
    names: set[str] = set()
    for line in block.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ASSIGN.match(line)
        if m:
            names.add(m.group(1))
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


def is_template_block(block: str, all_assigned: frozenset[str]) -> bool:
    """Return True when block contains an orchestrator-substituted placeholder token.

    A ``$X``/``${X}`` token never assigned in ANY bash block (and not a known-safe
    env/loop var), or an ``<identifier>`` angle-bracket placeholder, signals the
    block is a Claude-filled template / usage example, not verbatim shell.

    Args:
        block: referencing bash block body.
        all_assigned: union of variable names assigned across all blocks in the file.

    Returns:
        True if a never-assigned placeholder token is present.

    Examples:
        >>> is_template_block("cp x ${I}.md\\n", frozenset({"RUN_ID"}))
        True
        >>> is_template_block("echo ${RUN_ID}\\n", frozenset({"RUN_ID"}))
        False
        >>> is_template_block('grep "$SQ" rdeps <TARGET_MODULE>\\n', frozenset({"SQ"}))
        True
    """
    for line in block.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if _ANGLE_PLACEHOLDER.search(line):
            return True
        for m in _REF_ANY.finditer(line):
            tok = m.group(1)
            if tok in _SKIP_VARS or tok in all_assigned:
                continue
            return True
    return False


def reloads_before_ref(block: str, var: str) -> bool:
    """Return True if block re-loads persisted state before first reference to var.

    Detects ``eval "$(...)"`` and ``source``/``.`` sourcing lines appearing above
    the first reference — the state-file reload / re-derivation pattern.

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
    """
    idxs = _ref_line_indices(block, var)
    if not idxs:
        return False
    lines = block.splitlines()
    return any(_RELOAD.match(lines[i]) for i in range(idxs[0]))


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


def check_file(path: Path) -> list[str]:
    """Return C41 violation strings for one SKILL.md file.

    Args:
        path: path to SKILL.md to analyse.

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
    blocks = extract_bash_blocks(text)
    if len(blocks) < _MIN_BLOCKS:
        return []
    assigns = [assigned_vars(b) for b in blocks]
    all_assigned: frozenset[str] = frozenset().union(*assigns)
    findings: list[str] = []
    for i in range(1, len(blocks)):
        block = blocks[i]
        if is_template_block(block, all_assigned):  # rule 3: orchestrator-filled template
            continue
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
    parser = argparse.ArgumentParser(description="Detect shell variable persistence issues in SKILL.md files")
    parser.add_argument("files", nargs="*", help="SKILL.md files to check")
    parser.add_argument("--scan-dir", metavar="DIR", help="recursively scan DIR for SKILL.md files")
    parser.add_argument("--timeout", type=int, help="ignored — accepted for CLI compatibility")
    args = parser.parse_args(argv)

    paths: list[Path] = []
    if args.scan_dir:
        paths.extend(Path(args.scan_dir).rglob("*/skills/*/SKILL.md"))
    paths.extend(Path(f) for f in args.files)
    if not paths:
        paths.extend(Path(".").rglob("*/skills/*/SKILL.md"))

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
