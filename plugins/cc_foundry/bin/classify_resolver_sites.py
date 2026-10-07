#!/usr/bin/env python3
"""Classify resolver call sites as inline-required or safely cat-only extractable.

`bin/resolve_shared_path.py` and `bin/resolve_skill_subdir.py` are invoked from many
SKILL.md fenced bash blocks. A raw grep for the script name overcounts wildly: it
matches prose mentions, README rows, and doctest lines alongside real invocations. This
module answers the narrower question the efficiency audit needed -- of the *real*
invocation sites, how many resolve a variable that is used for nothing but ``cat``
afterward, and could therefore collapse to a plain ``cat`` one-liner?

A variable does not survive between Bash tool calls (each fenced ```bash block is a
separate subprocess), so a site is scoped to the single fence it is defined in --
usages after the fence closes are invisible to this classifier by construction, same
as they are to the shell itself.

Scope is deliberately narrow: only underscore-prefixed vars (`_FS`, `_SHARED`,
`_FOUNDRY_SHARED`...) count as sites. Plain-name vars (`AUDIT_TPL`, `DISTILL_MODES`...)
are a different, already-solved pattern -- several already cache themselves via a
`${TMPDIR}/.../audit-tpl` sentinel read-before-resolve, so folding them in here would
compare unlike things.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/classify_resolver_sites.py" plugins/cc_foundry
    python "${CLAUDE_PLUGIN_ROOT}/bin/classify_resolver_sites.py" plugins/cc_foundry --list-extractable
"""

from __future__ import annotations

import argparse
import pathlib
import re
from dataclasses import dataclass

#: Resolver scripts whose invocations in skill Markdown are counted and classified.
SCRIPTS = ("resolve_shared_path.py", "resolve_skill_subdir.py")

#: Matches a fenced ``bash`` block, capturing its body.
FENCE_RE = re.compile(r"```bash\n(.*?)```", re.DOTALL)
#: Matches a ``_VAR=...`` assignment whose value calls one of the resolver scripts, capturing variable and script.
ASSIGN_RE = re.compile(r"^\s*(_[A-Z][A-Z0-9_]*)\s*=.*(" + "|".join(re.escape(s) for s in SCRIPTS) + r")")


@dataclass
class Site:
    """One resolver invocation found inside a single bash fence.

    Attributes:
        file: Path the site was found in.
        var: Name of the variable the resolver output is assigned to.
        script: Which resolver script (`resolve_shared_path.py` or `resolve_skill_subdir.py`).
        extractable: True when every recognized use after the assignment is on a
            line starting with ``cat``, with empty-guard lines ignored.
    """

    file: pathlib.Path
    var: str
    script: str
    extractable: bool


def _strip_comment(line: str) -> str:
    """Drop a trailing ``# ...`` comment so it cannot masquerade as a use.

    Naive first-``#`` split -- correct for this corpus, where fenced bash lines never
    quote a literal ``#`` inside the strings this script cares about.

        >>> _strip_comment('cat "$VAR/foo.md"  # timeout: 5000')
        'cat "$VAR/foo.md"  '
        >>> _strip_comment('no comment here')
        'no comment here'
    """
    idx = line.find("#")
    return line if idx == -1 else line[:idx]


def classify_site(var: str, lines: list[str], start: int) -> bool:
    """Check whether recognized variable uses occur only on lines starting with cat.

    Scans every line after `start` within the same fence (`lines` is one fence's
    lines). A ``[ -z "$VAR" ]`` empty-guard line is dropped -- it is not a use, just a
    fail-fast check on the resolver's own success. Blank and comment-only lines are
    also not uses. Any surviving use on a line not starting with ``cat`` disqualifies
    extraction. This is a textual heuristic, not a shell parser: pipelines and later
    commands on a ``cat`` line are not analyzed. A var with **zero** uses is not extractable either -- that
    is a dead assignment, a different problem than a live cat-only one, and claiming it
    as a win would misreport a bug as a saving.

        >>> classify_site("_FS", ['_FS=$(python resolve_shared_path.py)', 'cat "$_FS/x.md"'], 0)
        True
        >>> lines = ['_FS=$(python resolve_shared_path.py)', '[ -z "$_FS" ] && exit 1', 'cat "$_FS/x.md"']
        >>> classify_site("_FS", lines, 0)
        True
        >>> classify_site("_FS", ['_FS=$(python resolve_shared_path.py)', 'echo "path: $_FS"'], 0)
        False
        >>> classify_site("_FS", ['_FS=$(python resolve_shared_path.py)'], 0)
        False
    """
    guard_re = re.compile(r'\[\s*-z\s*"\$\{?' + re.escape(var) + r'\}?"\s*\]')
    use_re = re.compile(r"\$\{?" + re.escape(var) + r"\}?\b")
    cat_re = re.compile(r"^\s*cat\s")

    found_use = False
    for raw in lines[start + 1 :]:
        line = _strip_comment(raw).strip()
        if not line or guard_re.search(line):
            continue
        if use_re.search(line):
            found_use = True
            if not cat_re.match(line):
                return False
    return found_use


def find_sites(text: str, file: pathlib.Path) -> list[Site]:
    """Find every resolver assignment inside `text`'s bash fences and classify each.

    Examples:
        >>> md = '```bash\\n_FS=$(python resolve_shared_path.py)\\ncat "$_FS/x.md"\\n```'
        >>> sites = find_sites(md, pathlib.Path("f.md"))
        >>> [(s.var, s.script, s.extractable) for s in sites]
        [('_FS', 'resolve_shared_path.py', True)]
    """
    sites: list[Site] = []
    for fence in FENCE_RE.findall(text):
        lines = fence.split("\n")
        for i, line in enumerate(lines):
            match = ASSIGN_RE.match(line)
            if not match:
                continue
            var, script = match.group(1), match.group(2)
            sites.append(Site(file, var, script, classify_site(var, lines, i)))
    return sites


def textual_count(text: str, script: str) -> int:
    """Count raw line-level mentions of `script`'s name -- prose included.

    This is the naive ``grep -c`` denominator; kept alongside `find_sites`'s
    extractable count so the two never get compared to each other by mistake.

        >>> textual_count("see resolve_shared_path.py and resolve_shared_path.py again", "resolve_shared_path.py")
        1
    """
    return sum(1 for line in text.splitlines() if script in line)


def scan(root: pathlib.Path) -> tuple[dict[str, int], list[Site]]:
    """Walk every `.md` file under `root`, aggregating textual counts and sites."""
    textual: dict[str, int] = dict.fromkeys(SCRIPTS, 0)
    sites: list[Site] = []
    for path in sorted(root.rglob("*.md")):
        text = path.read_text(errors="ignore")
        for script in SCRIPTS:
            textual[script] += textual_count(text, script)
        sites.extend(find_sites(text, path))
    return textual, sites


def main() -> None:
    """Print resolver-site counts and optionally list heuristic extraction candidates."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=pathlib.Path, help="plugin directory to scan, e.g. plugins/cc_foundry")
    parser.add_argument("--list-extractable", action="store_true", help="print each extractable site's file:var")
    args = parser.parse_args()

    textual, sites = scan(args.root)
    for script in SCRIPTS:
        script_sites = [s for s in sites if s.script == script]
        extractable = [s for s in script_sites if s.extractable]
        print(
            f"{script}: textual={textual[script]} invocation_sites={len(script_sites)} extractable={len(extractable)}"
        )
        if args.list_extractable:
            for s in extractable:
                print(f"  {s.file}: {s.var}")


if __name__ == "__main__":
    main()
