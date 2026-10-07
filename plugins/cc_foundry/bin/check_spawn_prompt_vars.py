#!/usr/bin/env python
"""check_spawn_prompt_vars.py — Detect $VAR references inside markdown spawn prompt templates.

Variables written as $VAR or ${VAR} inside ```markdown fenced blocks are passed
literally to spawned agents. The agent receives the dollar-sign string, not the
resolved value. Read tool calls on paths like "$_FOUNDRY_SHARED/foo.md" then fail
silently — no error surfaced at runtime.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_spawn_prompt_vars.py" [files...]
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_spawn_prompt_vars.py" --scan-dir plugins/

Output (stdout):
    One finding line per violation with prefix "C42-CRITICAL:", or a single pass line.

Exit codes:
    0   all files pass
    1   one or more violations found
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Variables that callers explicitly substitute into the prompt string before dispatch
_CALLER_SUBSTITUTED: frozenset[str] = frozenset(
    {
        "ARGUMENTS",
        "RUN_DIR",
        "AUDIT_TPL",
        "REPORT_DIR",
        "SCOPE",
        "SKILL_DIR",
        "PLUGIN",
        "NAME",
        "DIRECTIVE",
    }
)

#: Well-known env vars the spawned subagent resolves in its OWN environment — a bare
#: $TMPDIR / ${HOME} etc. is not orchestrator-context payload, so never flag them.
_WELL_KNOWN_ENV: frozenset[str] = frozenset({"TMPDIR", "HOME", "PWD", "CLAUDE_PLUGIN_ROOT"})

#: Matches the opening fence of a ``markdown`` code block, which holds a spawn prompt.
_MD_OPEN = re.compile(r"^```markdown\s*$")
#: Matches a bare closing code fence.
_FENCE_CLOSE = re.compile(r"^```\s*$")
#: Matches a ``$VAR`` or ``${VAR}`` reference with a name of at least two characters.
_VAR_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]+)\}?")
#: Parameter-expansion-with-default idiom: ${VAR:-default}. Portable shell the subagent
#: reproduces and expands in its own shell — never an unexpanded-literal bug.
_DEFAULT_EXPANSION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]+):-[^}]*\}")
#: A directive line telling the orchestrator to resolve a token before dispatch.
_DIRECTIVE_WORDS = re.compile(r"(?i)\b(?:expand|substitute|replace)\b")
#: "env var" / "environment variable" — the $VAR on the line is a documented env-var name.
_ENV_VAR_PHRASE = re.compile(r"(?i)\benv(?:ironment)?\s+var(?:iable)?s?\b")
#: Editorial square-bracket span, e.g. [Continue with template from $TEMPLATE_FILE].
_BRACKET_SPAN = re.compile(r"\[[^\[\]]*\]")


def scan_file_context(text: str) -> tuple[set[str], set[str]]:
    """Pre-scan the whole file for var names that must never be flagged.

    Args:
        text: full file contents.

    Returns:
        Tuple ``(default_vars, directive_vars)`` where ``default_vars`` appear in a
        ``${VAR:-default}`` idiom somewhere in the file, and ``directive_vars`` appear
        on a line that also carries an expand/substitute/replace instruction.

    Examples:
        >>> d, s = scan_file_context("use ${TMP:-/x}\\nexpand $FOO before passing\\n")
        >>> sorted(d), sorted(s)
        (['TMP'], ['FOO'])
    """
    default_vars: set[str] = set()
    directive_vars: set[str] = set()
    for line in text.splitlines():
        for dm in _DEFAULT_EXPANSION.finditer(line):
            default_vars.add(dm.group(1))
        if _DIRECTIVE_WORDS.search(line):
            for vm in _VAR_REF.finditer(line):
                directive_vars.add(vm.group(1))
    return default_vars, directive_vars


def is_suppressed(var: str, start: int, line: str, default_vars: set[str], directive_vars: set[str]) -> bool:
    """Return True when a ``$VAR`` occurrence is a known false-positive.

    Args:
        var: variable name (without ``$`` / braces).
        start: column offset of the match on the line.
        line: full source line the match was found on.
        default_vars: vars seen in ``${VAR:-default}`` form file-wide.
        directive_vars: vars named on an expand/substitute/replace directive line file-wide.

    Returns:
        True if the occurrence should be suppressed, False if it must be flagged.

    Examples:
        >>> is_suppressed("TMPDIR", 0, "$TMPDIR/x", set(), set())
        True
        >>> is_suppressed("FOO", 0, "$FOO/x", set(), set())
        False
    """
    if var in _CALLER_SUBSTITUTED or var in _WELL_KNOWN_ENV:
        return True
    if var in default_vars or var in directive_vars:
        return True
    if _ENV_VAR_PHRASE.search(line):
        return True
    return any(b0 <= start < b1 for b0, b1 in (m.span() for m in _BRACKET_SPAN.finditer(line)))


def check_file(path: Path) -> list[str]:
    """Return C42 violation strings for one SKILL.md or template file.

    Args:
        path: path to file to analyse.

    Returns:
        List of finding strings; empty list = no violations.

    Examples:
        >>> import os, tempfile
        >>> content = "```markdown\\nRead $_FOUNDRY_SHARED/foo.md\\n```\\n"
        >>> with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
        ...     _ = f.write(content); name = f.name
        >>> findings = check_file(Path(name)); os.unlink(name)
        >>> any("C42" in x and "_FOUNDRY_SHARED" in x for x in findings)
        True
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    default_vars, directive_vars = scan_file_context(text)
    findings: list[str] = []
    in_md = False
    block_num = 0
    reported: set[tuple[int, str]] = set()

    for line in text.splitlines():
        if not in_md and _MD_OPEN.match(line):
            in_md = True
            block_num += 1
            continue
        if in_md and _FENCE_CLOSE.match(line):
            in_md = False
            continue
        if not in_md:
            continue
        for m in _VAR_REF.finditer(line):
            var = m.group(1)
            if len(var) <= 1 or is_suppressed(var, m.start(), line, default_vars, directive_vars):
                continue
            key = (block_num, var)
            if key in reported:
                continue
            reported.add(key)
            findings.append(
                f"C42-CRITICAL: {path} (markdown block {block_num}):"
                f" ${var} unexpanded — variable reaches spawned agent as literal string"
            )

    return findings


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — scan files and print findings.

    Args:
        argv: argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code: 0 = all pass, 1 = violations found.
    """
    parser = argparse.ArgumentParser(description="Detect unexpanded $VAR in markdown spawn prompt blocks")
    parser.add_argument("files", nargs="*", help="files to check")
    parser.add_argument("--scan-dir", metavar="DIR", help="recursively scan DIR for SKILL.md and template .md files")
    parser.add_argument("--timeout", type=int, help="ignored — accepted for CLI compatibility")
    args = parser.parse_args(argv)

    paths: list[Path] = []
    if args.scan_dir:
        root = Path(args.scan_dir)
        paths.extend(root.rglob("*/skills/*/SKILL.md"))
        paths.extend(root.rglob("*/skills/*/templates/*.md"))
    paths.extend(Path(f) for f in args.files)
    if not paths:
        root = Path(".")
        paths.extend(root.rglob("*/skills/*/SKILL.md"))
        paths.extend(root.rglob("*/skills/*/templates/*.md"))

    all_findings: list[str] = []
    for p in sorted(set(paths)):
        all_findings.extend(check_file(p))

    if all_findings:
        print("\n".join(all_findings))
        return 1
    print("✓: Check 42 — no unexpanded variables in spawn prompt markdown blocks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
